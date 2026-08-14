"""Smoke tests. Run with: python -m tests.test_smoke  (or: pytest tests/)"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["ONEAI_SKIP_DOTENV"] = "1"   # ignore the developer's .env
os.environ["DEFAULT_MODE"] = "panel"    # these suites exercise the panel path

TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "test.db")
os.environ["OPENROUTER_KEY"] = "sk-or-test"
os.environ["SECRET_KEY"] = "test-secret"

import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import app as app_module  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(f"{'PASS' if condition else 'FAIL'}  {name}{'  -> ' + detail if detail and not condition else ''}")


def fake_generate(prompt, history=None, attachments=None):
    return llm.PanelResult(
        answer=f"Echo({len(history or [])} prior): {prompt[:40]}",
        nodes={"logic": "x", "technical": "y", "clarity": "z"},
    )


def main():
    llm.generate = fake_generate
    application = app_module.app
    application.config["TESTING"] = True
    client = application.test_client()

    # --- health & home ---
    check("GET /healthz", client.get("/healthz").status_code == 200)
    check("GET / renders template", client.get("/").status_code == 200)

    # --- empty state ---
    r = client.get("/api/chats")
    check("GET /api/chats empty", r.status_code == 200 and r.get_json() == [])

    # --- create a chat via /api/chat ---
    r = client.post("/api/chat", json={"message": "What is a B-tree?", "chat_id": None})
    body = r.get_json()
    check("POST /api/chat creates chat", r.status_code == 200 and isinstance(body["chat_id"], int),
          str(body))
    chat_id = body["chat_id"]
    check("answer returned", "Echo" in body["master_answer"])
    check("degraded flag present", body["degraded"] is False)

    # --- history is replayed on the second turn ---
    r = client.post("/api/chat", json={"message": "And a B+ tree?", "chat_id": chat_id})
    check("history replayed to model", "Echo(2 prior)" in r.get_json()["master_answer"],
          r.get_json()["master_answer"])

    # --- messages persisted with normalised roles ---
    msgs = client.get(f"/api/chats/{chat_id}").get_json()
    roles = [m["role"] for m in msgs]
    check("4 messages stored", len(msgs) == 4, str(roles))
    check("roles normalised", roles == ["user", "assistant", "user", "assistant"], str(roles))

    # --- temp chat is not persisted ---
    before = len(client.get("/api/chats").get_json())
    r = client.post("/api/chat", json={"message": "secret", "chat_id": "temp"})
    after = len(client.get("/api/chats").get_json())
    check("temp chat not persisted", r.get_json()["chat_id"] == "temp" and before == after)

    # --- title generated from prompt ---
    chats = client.get("/api/chats").get_json()
    check("title from prompt", chats[0]["title"].startswith("What is a B-tree"), str(chats[0]))

    # --- search ---
    r = client.get("/api/chats/search?q=B-tree")
    check("search finds chat", r.status_code == 200 and len(r.get_json()) == 1, str(r.get_json()))
    r = client.get("/api/chats/search?q=%")
    check("LIKE wildcard escaped", r.get_json() == [], str(r.get_json()))
    check("empty search returns []", client.get("/api/chats/search?q=").get_json() == [])

    # --- rename ---
    r = client.patch(f"/api/chats/{chat_id}", json={"title": "Data structures"})
    check("rename works", r.status_code == 200)
    check("rename persisted",
          client.get("/api/chats").get_json()[0]["title"] == "Data structures")
    check("rename rejects empty",
          client.patch(f"/api/chats/{chat_id}", json={"title": "   "}).status_code == 400)
    check("rename 404s on missing chat",
          client.patch("/api/chats/99999", json={"title": "x"}).status_code == 404)

    # --- validation ---
    check("empty message rejected",
          client.post("/api/chat", json={"message": "  "}).status_code == 400)
    check("non-string message rejected",
          client.post("/api/chat", json={"message": 42}).status_code == 400)
    check("oversized message rejected",
          client.post("/api/chat", json={"message": "x" * 20000}).status_code == 400)
    check("bad chat_id rejected",
          client.post("/api/chat", json={"message": "hi", "chat_id": "abc"}).status_code == 400)
    check("unknown chat_id 404s",
          client.post("/api/chat", json={"message": "hi", "chat_id": 99999}).status_code == 404)
    check("non-JSON body rejected",
          client.post("/api/chat", data="not json",
                      content_type="application/json").status_code == 400)
    check("GET missing chat 404s", client.get("/api/chats/99999").status_code == 404)

    # --- LLM failure must not persist a fake answer ---
    def boom(prompt, history=None, attachments=None):
        raise llm.LLMError("all models down")

    llm.generate = boom
    n_before = len(client.get(f"/api/chats/{chat_id}").get_json())
    r = client.post("/api/chat", json={"message": "will fail", "chat_id": chat_id})
    n_after = len(client.get(f"/api/chats/{chat_id}").get_json())
    check("LLM failure returns 503", r.status_code == 503, str(r.get_json()))
    check("failure not saved as assistant reply", n_after == n_before + 1,
          f"{n_before} -> {n_after}")
    llm.generate = fake_generate

    # --- cascade delete ---
    r = client.delete(f"/api/chats/{chat_id}")
    check("delete works", r.status_code == 200)
    with db.get_conn() as conn:
        orphans = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()["c"]
    check("messages cascade-deleted", orphans == 0, f"{orphans} orphans")
    check("delete 404s on missing chat", client.delete("/api/chats/99999").status_code == 404)

    # --- legacy routes still work ---
    check("legacy /get_chats", client.get("/get_chats").status_code == 200)
    check("legacy /chat", client.post("/chat", json={"message": "hey"}).status_code == 200)

    # --- migration from the old schema ---
    legacy = TMP / "legacy.db"
    conn = sqlite3.connect(legacy)
    conn.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT)")
    conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "chat_id INTEGER, role TEXT, content TEXT)")
    conn.execute("INSERT INTO chats (title) VALUES ('Old chat')")
    conn.execute("INSERT INTO messages (chat_id, role, content) VALUES (1,'User','hi')")
    conn.execute("INSERT INTO messages (chat_id, role, content) VALUES (1,'One AI','hello')")
    conn.commit()
    conn.close()

    config.DB_PATH = legacy
    db.init_db()
    migrated = db.get_messages(1)
    check("legacy db migrates", [m["role"] for m in migrated] == ["user", "assistant"],
          str([m["role"] for m in migrated]))
    check("legacy titles preserved", db.list_chats()[0]["title"] == "Old chat")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failures:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
