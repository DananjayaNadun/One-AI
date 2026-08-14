"""SQLite access layer.

Design notes:
- One connection per request (SQLite connections are not thread-safe, and Flask
  serves requests on multiple threads).
- WAL mode so a reader is never blocked by the writer.
- Foreign keys ON with ON DELETE CASCADE, so deleting a chat cannot orphan
  messages. The original code deleted from two tables without a transaction,
  which could leave orphans if the second statement failed.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

import config

log = logging.getLogger(__name__)

TABLES = """
CREATE TABLE IF NOT EXISTS chats (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT    NOT NULL DEFAULT 'New chat',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT    NOT NULL,
    meta       TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Attachments live here rather than on disk so that deleting a chat cannot
-- leave orphaned files behind, and so a PythonAnywhere redeploy does not wipe
-- them. message_id is NULL between upload and send.
CREATE TABLE IF NOT EXISTS attachments (
    id         TEXT    PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    name       TEXT    NOT NULL,
    kind       TEXT    NOT NULL,
    size       INTEGER NOT NULL DEFAULT 0,
    text       TEXT    NOT NULL DEFAULT '',
    data_url   TEXT    NOT NULL DEFAULT '',
    truncated  INTEGER NOT NULL DEFAULT 0,
    meta       TEXT    NOT NULL DEFAULT '{}',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

# Indexes are applied after migration, because on a database created by the
# original version the columns they reference do not exist yet.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_attachments_message ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_attachments_created ON attachments(created_at);
CREATE INDEX IF NOT EXISTS idx_chats_updated ON chats(updated_at DESC);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Yield a connection, committing on success and rolling back on error."""
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(TABLES)
        _migrate(conn)
        conn.executescript(INDEXES)
    log.info("Database ready at %s", config.DB_PATH)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release, if missing.

    This lets an existing database.db from the old version keep working instead
    of throwing 'no such column' at runtime.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(chats)")}
    for column, ddl in (
        ("created_at", "ALTER TABLE chats ADD COLUMN created_at TEXT"),
        ("updated_at", "ALTER TABLE chats ADD COLUMN updated_at TEXT"),
    ):
        if column not in existing:
            conn.execute(ddl)
            conn.execute(f"UPDATE chats SET {column} = datetime('now') WHERE {column} IS NULL")

    msg_cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)")}
    if "meta" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")
    if "created_at" not in msg_cols:
        conn.execute("ALTER TABLE messages ADD COLUMN created_at TEXT")
        conn.execute("UPDATE messages SET created_at = datetime('now') WHERE created_at IS NULL")

    # The old schema stored roles as 'User' / 'One AI'. Normalise them so the
    # history replay and the CHECK constraint agree.
    conn.execute("UPDATE messages SET role = 'user' WHERE role IN ('User', 'USER')")
    conn.execute(
        "UPDATE messages SET role = 'assistant' "
        "WHERE role NOT IN ('user', 'assistant')"
    )


# --- Queries -----------------------------------------------------------------

def list_chats(limit: int = 200) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM chats "
            "ORDER BY COALESCE(updated_at, created_at) DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def search_chats(query: str, limit: int = 100) -> list[dict[str, Any]]:
    # Escape LIKE wildcards so a literal % or _ in the query does not match
    # everything. The original code passed user input straight into the pattern.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    term = f"%{escaped}%"
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT c.id, c.title, c.updated_at
            FROM chats c
            LEFT JOIN messages m ON m.chat_id = c.id
            WHERE m.content LIKE ? ESCAPE '\\' OR c.title LIKE ? ESCAPE '\\'
            ORDER BY COALESCE(c.updated_at, c.created_at) DESC, c.id DESC
            LIMIT ?
            """,
            (term, term, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def chat_exists(chat_id: int) -> bool:
    with get_conn() as conn:
        return conn.execute("SELECT 1 FROM chats WHERE id = ?", (chat_id,)).fetchone() is not None


def get_messages(chat_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, role, content, meta, created_at FROM messages "
            "WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
        messages = [dict(r) for r in rows]
        if messages:
            ids = [m["id"] for m in messages]
            placeholders = ",".join("?" * len(ids))
            atts = conn.execute(
                f"SELECT message_id, id, name, kind, size, truncated, data_url, meta "
                f"FROM attachments WHERE message_id IN ({placeholders}) ORDER BY rowid",
                ids,
            ).fetchall()
    grouped: dict[int, list] = {}
    for row in atts if messages else []:
        record = dict(row)
        grouped.setdefault(record.pop("message_id"), []).append(record)
    for message in messages:
        message["attachments"] = grouped.get(message["id"], [])
        try:
            message["meta"] = json.loads(message.get("meta") or "{}")
        except (TypeError, ValueError):
            message["meta"] = {}
    return messages


def get_recent_messages(chat_id: int, limit: int) -> list[dict[str, Any]]:
    """Last N messages in chronological order, for replaying context."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def get_message(message_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, chat_id, role, content FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
    return dict(row) if row else None


def preceding_user_message(chat_id: int, message_id: int) -> dict[str, Any] | None:
    """The user turn that produced a given assistant message."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, role, content FROM messages "
            "WHERE chat_id = ? AND id < ? AND role = 'user' ORDER BY id DESC LIMIT 1",
            (chat_id, message_id),
        ).fetchone()
    return dict(row) if row else None


def truncate_from(chat_id: int, message_id: int) -> int:
    """Delete a message and everything after it in the same chat.

    Used by edit and regenerate. Without this, re-answering appends a duplicate
    exchange instead of replacing the old one, and the duplicated turns are then
    replayed to the model as context.
    """
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM messages WHERE chat_id = ? AND id >= ?", (chat_id, message_id)
        )
        conn.execute("UPDATE chats SET updated_at = datetime('now') WHERE id = ?", (chat_id,))
        return cur.rowcount


def history_before(chat_id: int, message_id: int, limit: int) -> list[dict[str, Any]]:
    """Context for a re-answer: the turns preceding a given message."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? AND id < ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, message_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]


def create_chat(title: str) -> int:
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO chats (title) VALUES (?)", (title,))
        return int(cur.lastrowid)


def add_message(chat_id: int, role: str, content: str, meta: dict | None = None) -> int:
    if role not in ("user", "assistant"):
        raise ValueError(f"invalid role: {role!r}")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO messages (chat_id, role, content, meta) VALUES (?, ?, ?, ?)",
            (chat_id, role, content, json.dumps(meta or {})),
        )
        conn.execute("UPDATE chats SET updated_at = datetime('now') WHERE id = ?", (chat_id,))
        return int(cur.lastrowid)


def rename_chat(chat_id: int, title: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE chats SET title = ?, updated_at = datetime('now') WHERE id = ?",
            (title, chat_id),
        )
        return cur.rowcount > 0


def delete_chat(chat_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        return cur.rowcount > 0


# --- Attachments -------------------------------------------------------------

def save_attachment(att) -> None:
    """Persist a freshly ingested attachment, not yet bound to a message."""
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO attachments (id, name, kind, size, text, data_url, truncated, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                att.id, att.name, att.kind, att.size, att.text,
                att.data_url, int(att.truncated), json.dumps(att.meta),
            ),
        )


def get_attachments(ids: list[str]) -> list[dict]:
    """Fetch attachments by id, preserving the order the caller asked for."""
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM attachments WHERE id IN ({placeholders})", ids
        ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def attachments_for_message(message_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name, kind, size, truncated, data_url, meta "
            "FROM attachments WHERE message_id = ? ORDER BY rowid",
            (message_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def bind_attachments(message_id: int, ids: list[str]) -> None:
    if not ids:
        return
    with get_conn() as conn:
        conn.executemany(
            "UPDATE attachments SET message_id = ? WHERE id = ? AND message_id IS NULL",
            [(message_id, i) for i in ids],
        )


def purge_orphan_attachments(ttl_seconds: int) -> int:
    """Delete uploads that were never sent. Called on startup."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM attachments WHERE message_id IS NULL "
            "AND created_at < datetime('now', ?)",
            (f"-{int(ttl_seconds)} seconds",),
        )
        return cur.rowcount
