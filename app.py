"""One AI -- Flask application."""
from __future__ import annotations

import logging
import os
import re
import secrets
import time

import json as _json

from flask import Flask, Response, jsonify, render_template, request, stream_with_context
from flask_cors import CORS

import attachments as att_lib
import config
import db
import llm

logging.basicConfig(
    level=logging.DEBUG if config.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("one_ai")


def create_app() -> Flask:
    for warning in config.validate():
        log.warning(warning)

    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY or secrets.token_hex(32)
    app.config["JSON_SORT_KEYS"] = False
    # Must exceed the per-file limit, since a message may carry several files.
    app.config["MAX_CONTENT_LENGTH"] = (
        config.MAX_UPLOAD_BYTES * config.MAX_FILES_PER_MESSAGE
    ) + (2 * 1024 * 1024)

    if config.CORS_ORIGINS:
        CORS(app, resources={r"/api/*": {"origins": config.CORS_ORIGINS}})

    db.init_db()
    purged = db.purge_orphan_attachments(config.ATTACHMENT_TTL_SECONDS)
    if purged:
        log.info("purged %d unsent attachment(s)", purged)
    register_routes(app)
    register_error_handlers(app)
    return app


# --- Validation helpers ------------------------------------------------------

class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest("Request body must be a JSON object.")
    return data


def clean_text(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise BadRequest(f"'{field}' must be a string.")
    text = value.strip()
    if not text:
        raise BadRequest(f"'{field}' cannot be empty.")
    if len(text) > max_chars:
        raise BadRequest(f"'{field}' is too long (max {max_chars} characters).")
    return text


def parse_chat_id(value: object) -> int | None:
    """Return an int chat id, or None for a new/temporary chat.

    The frontend sends null for 'new chat' and the string "temp" for a chat
    that must not be persisted. The old code compared with != "temp" and then
    used the value as an int, so a temp chat could still reach SQL.
    """
    if value is None or value == "" or value == "temp":
        return None
    try:
        chat_id = int(value)
    except (TypeError, ValueError):
        raise BadRequest("'chat_id' must be an integer, null, or \"temp\".")
    if chat_id <= 0:
        raise BadRequest("'chat_id' must be positive.")
    return chat_id


def parse_attachment_ids(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BadRequest("'attachment_ids' must be a list.")
    if len(value) > config.MAX_FILES_PER_MESSAGE:
        raise BadRequest(f"At most {config.MAX_FILES_PER_MESSAGE} files per message.")
    ids = []
    for item in value:
        if not isinstance(item, str) or not item.isalnum() or len(item) != 32:
            raise BadRequest("Invalid attachment id.")
        ids.append(item)
    return ids


def attachment_payload(records: list[dict]) -> list[dict]:
    return [
        {
            "id": r["id"], "name": r["name"], "kind": r["kind"],
            "data_url": r["data_url"] if r["kind"] == "image" else "",
        }
        for r in records
    ]


def parse_mode(value: object) -> str:
    if value is None:
        return config.DEFAULT_MODE
    if value not in config.MODES:
        raise BadRequest(f"'mode' must be one of {', '.join(config.MODES)}.")
    return str(value)


# Requests cost is per mode: the panel makes four calls, single-model modes one.
def requests_for(mode: str) -> int:
    return 4 if mode == "panel" else 1


def receipt(result, seconds: float) -> dict:
    """What the consensus strip needs, stored alongside the message."""
    return {
        "nodes": sorted(result.nodes.keys()),
        "seconds": max(1, round(seconds)),
        "degraded": bool(result.degraded),
        "mode": getattr(result, "mode", "panel"),
        "model": getattr(result, "model", ""),
        "truncated": bool(getattr(result, "truncated", False)),
        "requests": requests_for(getattr(result, "mode", "panel")),
    }


def answer_or_503(prompt: str, history: list[dict], records: list[dict],
                  mode: str = "panel"):
    """Generate an answer, converting model failure into a clean 503.

    Nothing is persisted on failure -- an error string saved as an assistant
    message would poison both the transcript and the context sent next time.
    """
    started = time.monotonic()
    try:
        if mode == "panel":
            result = llm.generate(prompt, history, records)
        else:
            result = llm.generate_single(prompt, history, records, mode)
        result.seconds = time.monotonic() - started
        return result
    except llm.RateLimited as exc:
        log.warning("rate limited: %s", exc)
        raise BadRequest(str(exc), status=429)
    except llm.LLMError as exc:
        log.error("generation failed: %s", exc)
        raise BadRequest(
            "No free model could answer right now. The free roster changes "
            "often, so this usually means the models in your .env were "
            "delisted. Restart to refresh the catalog, or clear CODE_MODELS "
            "and let discovery pick.",
            status=503,
        )


def make_title(prompt: str) -> str:
    first_line = prompt.strip().splitlines()[0]
    title = first_line[:60].strip()
    if len(first_line) > 60:
        title += "\u2026"
    return title or "New chat"


# --- Routes ------------------------------------------------------------------

def register_routes(app: Flask) -> None:

    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "database": str(config.DB_PATH)})

    @app.get("/api/chats")
    def get_chats():
        return jsonify(db.list_chats())

    @app.get("/api/chats/search")
    def search_chats():
        query = (request.args.get("q") or "").strip()
        if not query:
            return jsonify([])
        return jsonify(db.search_chats(query))

    @app.get("/api/chats/<int:chat_id>")
    def get_chat(chat_id: int):
        if not db.chat_exists(chat_id):
            raise BadRequest("Chat not found.", status=404)
        return jsonify(db.get_messages(chat_id))

    @app.patch("/api/chats/<int:chat_id>")
    def rename_chat(chat_id: int):
        title = clean_text(json_body().get("title"), "title", config.MAX_TITLE_CHARS)
        if not db.rename_chat(chat_id, title):
            raise BadRequest("Chat not found.", status=404)
        return jsonify({"status": "ok", "id": chat_id, "title": title})

    @app.delete("/api/chats/<int:chat_id>")
    def delete_chat(chat_id: int):
        if not db.delete_chat(chat_id):
            raise BadRequest("Chat not found.", status=404)
        return jsonify({"status": "ok", "id": chat_id})

    @app.post("/api/upload")
    def upload():
        files = request.files.getlist("files")
        if not files:
            raise BadRequest("No files were uploaded.")
        if len(files) > config.MAX_FILES_PER_MESSAGE:
            raise BadRequest(f"At most {config.MAX_FILES_PER_MESSAGE} files at a time.")

        accepted, rejected = [], []
        for storage in files:
            name = storage.filename or "file"
            try:
                raw = storage.read()
                attachment = att_lib.ingest(name, raw)
                db.save_attachment(attachment)
                accepted.append(attachment.descriptor())
            except att_lib.AttachmentError as exc:
                rejected.append({"name": name, "error": str(exc)})
            except Exception:
                log.exception("upload failed for %s", name)
                rejected.append({"name": name, "error": "Could not process this file."})

        if not accepted and rejected:
            # Surface the first real reason rather than a generic failure.
            return jsonify({"attachments": [], "rejected": rejected}), 415

        return jsonify({"attachments": accepted, "rejected": rejected})

    @app.post("/api/chat")
    def chat():
        data = json_body()
        attachment_ids = parse_attachment_ids(data.get("attachment_ids"))
        records = db.get_attachments(attachment_ids)
        if len(records) != len(attachment_ids):
            raise BadRequest("One or more attachments have expired. Please re-upload.")

        raw_message = data.get("message")
        if attachment_ids and isinstance(raw_message, str) and not raw_message.strip():
            # A bare file with no question is a legitimate request.
            raw_message = "Please review the attached file(s)."
        prompt = clean_text(raw_message, "message", config.MAX_PROMPT_CHARS)
        chat_id = parse_chat_id(data.get("chat_id"))
        mode = parse_mode(data.get("mode"))
        ephemeral = data.get("chat_id") == "temp"

        history: list[dict[str, str]] = []

        if not ephemeral:
            if chat_id is None:
                chat_id = db.create_chat(make_title(prompt))
            elif not db.chat_exists(chat_id):
                raise BadRequest("Chat not found.", status=404)
            else:
                history = [
                    {"role": m["role"], "content": m["content"]}
                    for m in db.get_recent_messages(chat_id, config.HISTORY_TURNS)
                ]
            user_message_id = db.add_message(chat_id, "user", prompt)
            db.bind_attachments(user_message_id, attachment_ids)
        else:
            user_message_id = None

        result = answer_or_503(prompt, history, records, mode)
        meta = receipt(result, result.seconds)

        assistant_id = None
        if not ephemeral:
            assistant_id = db.add_message(chat_id, "assistant", result.answer, meta)

        return jsonify({
            "master_answer": result.answer,
            "chat_id": "temp" if ephemeral else chat_id,
            "message_id": assistant_id,
            "user_message_id": None if ephemeral else user_message_id,
            **meta,
            "attachments": attachment_payload(records),
        })

    @app.post("/api/chats/<int:chat_id>/regenerate")
    def regenerate(chat_id: int):
        """Replace an assistant message instead of appending a new exchange."""
        data = json_body()
        message_id = data.get("message_id")
        if not isinstance(message_id, int):
            raise BadRequest("'message_id' must be an integer.")

        target = db.get_message(message_id)
        if not target or target["chat_id"] != chat_id:
            raise BadRequest("Message not found.", status=404)
        if target["role"] != "assistant":
            raise BadRequest("Only assistant messages can be regenerated.")

        source = db.preceding_user_message(chat_id, message_id)
        if not source:
            raise BadRequest("Nothing to regenerate from.")

        records = db.get_attachments(
            [a["id"] for a in db.attachments_for_message(source["id"])]
        )
        history = db.history_before(chat_id, source["id"], config.HISTORY_TURNS)
        result = answer_or_503(source["content"], history, records,
                               parse_mode(data.get("mode")))

        db.truncate_from(chat_id, message_id)
        meta = receipt(result, result.seconds)
        assistant_id = db.add_message(chat_id, "assistant", result.answer, meta)

        return jsonify({
            "master_answer": result.answer,
            "chat_id": chat_id,
            "message_id": assistant_id,
            **meta,
        })

    @app.post("/api/chats/<int:chat_id>/edit")
    def edit_message(chat_id: int):
        """Rewrite a user message and re-answer, dropping everything after it."""
        data = json_body()
        message_id = data.get("message_id")
        if not isinstance(message_id, int):
            raise BadRequest("'message_id' must be an integer.")
        prompt = clean_text(data.get("message"), "message", config.MAX_PROMPT_CHARS)

        target = db.get_message(message_id)
        if not target or target["chat_id"] != chat_id:
            raise BadRequest("Message not found.", status=404)
        if target["role"] != "user":
            raise BadRequest("Only your own messages can be edited.")

        keep_ids = [a["id"] for a in db.attachments_for_message(message_id)]
        records = db.get_attachments(keep_ids)
        history = db.history_before(chat_id, message_id, config.HISTORY_TURNS)
        result = answer_or_503(prompt, history, records,
                               parse_mode(data.get("mode")))

        db.truncate_from(chat_id, message_id)
        new_user_id = db.add_message(chat_id, "user", prompt)
        db.bind_attachments(new_user_id, keep_ids)
        meta = receipt(result, result.seconds)
        assistant_id = db.add_message(chat_id, "assistant", result.answer, meta)

        return jsonify({
            "master_answer": result.answer,
            "chat_id": chat_id,
            "user_message_id": new_user_id,
            "message_id": assistant_id,
            **meta,
            "attachments": attachment_payload(records),
        })

    @app.post("/api/chat/stream")
    def chat_stream():
        """Server-sent events for single-model modes.

        The panel has nothing to stream: no useful text exists until the
        aggregator runs, by which point the answer is already complete.
        """
        if not config.STREAMING:
            raise BadRequest("Streaming is disabled. Set STREAMING=true in .env.", status=409)

        data = json_body()
        mode = parse_mode(data.get("mode"))
        if mode == "panel":
            raise BadRequest("Panel mode cannot stream. Use direct or code mode.")

        attachment_ids = parse_attachment_ids(data.get("attachment_ids"))
        records = db.get_attachments(attachment_ids)
        if len(records) != len(attachment_ids):
            raise BadRequest("One or more attachments have expired. Please re-upload.")

        raw = data.get("message")
        if attachment_ids and isinstance(raw, str) and not raw.strip():
            raw = "Please review the attached file(s)."
        prompt = clean_text(raw, "message", config.MAX_PROMPT_CHARS)
        chat_id = parse_chat_id(data.get("chat_id"))
        ephemeral = data.get("chat_id") == "temp"

        history: list[dict[str, str]] = []
        user_message_id = None
        if not ephemeral:
            if chat_id is None:
                chat_id = db.create_chat(make_title(prompt))
            elif not db.chat_exists(chat_id):
                raise BadRequest("Chat not found.", status=404)
            else:
                history = [
                    {"role": m["role"], "content": m["content"]}
                    for m in db.get_recent_messages(chat_id, config.HISTORY_TURNS)
                ]
            user_message_id = db.add_message(chat_id, "user", prompt)
            db.bind_attachments(user_message_id, attachment_ids)

        def event(name: str, payload: dict) -> str:
            return f"event: {name}\ndata: {_json.dumps(payload)}\n\n"

        def generate():
            yield event("start", {
                "chat_id": "temp" if ephemeral else chat_id,
                "user_message_id": user_message_id,
                "attachments": attachment_payload(records),
            })

            started = time.monotonic()
            collected: list[str] = []
            used_model = ""
            failed = None

            try:
                for piece in llm.stream_single(prompt, history, records, mode):
                    if piece["type"] == "chunk":
                        collected.append(piece["text"])
                        yield event("chunk", {"text": piece["text"]})
                    elif piece["type"] == "done":
                        used_model = piece.get("model", "")
                    elif piece["type"] == "error":
                        failed = piece["message"]
            except Exception:
                log.exception("stream failed")
                failed = "The model service dropped the connection."

            answer = "".join(collected).strip()

            # A stream that produced nothing must not be saved as an answer.
            if not answer:
                yield event("error", {"message": failed or "The model returned nothing."})
                return

            meta = {
                "nodes": [], "seconds": max(1, round(time.monotonic() - started)),
                "degraded": False, "mode": mode, "model": used_model, "truncated": False,
            }
            message_id = None
            if not ephemeral:
                message_id = db.add_message(chat_id, "assistant", answer, meta)

            yield event("done", {"message_id": message_id, **meta,
                                 "partial_error": failed})

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/config")
    def client_config():
        return jsonify({
            "modes": list(config.MODES),
            "default_mode": config.DEFAULT_MODE,
            "streaming": config.STREAMING,
            "max_files": config.MAX_FILES_PER_MESSAGE,
            "free_only": config.FREE_ONLY,
            "free_rpm": config.FREE_RPM,
            "free_rpd": config.FREE_RPD,
            "requests_per_message": {m: requests_for(m) for m in config.MODES},
        })

    @app.get("/api/models")
    def list_models():
        """What the app would actually use, in order, per mode.

        Useful when a mode starts failing: it shows immediately whether the
        models in your .env still exist on the free roster.
        """
        import models as catalog

        def describe(ids):
            return [
                {
                    "id": mid,
                    "free": catalog.is_free(mid),
                    "trains_on_free": catalog.trains_on_free(mid),
                }
                for mid in ids[:8]
            ]

        return jsonify({
            "catalog_loaded": bool(catalog.get_catalog()),
            "free_only": config.FREE_ONLY,
            "code": describe(catalog.code_models()),
            "text": describe(catalog.text_models()),
            "vision": describe(catalog.vision_models()),
        })

    @app.get("/api/chats/<int:chat_id>/export")
    def export_chat(chat_id: int):
        if not db.chat_exists(chat_id):
            raise BadRequest("Chat not found.", status=404)
        chat = next((c for c in db.list_chats(limit=10_000) if c["id"] == chat_id), None)
        title = (chat or {}).get("title", "Chat")

        lines = [f"# {title}", ""]
        for message in db.get_messages(chat_id):
            who = "You" if message["role"] == "user" else "One AI"
            stamp = message.get("created_at") or ""
            lines.append(f"## {who}" + (f"  \n*{stamp}*" if stamp else ""))
            names = [a["name"] for a in message.get("attachments", [])]
            if names:
                lines.append(f"> Attached: {', '.join(names)}")
                lines.append("")
            lines.append(message["content"])
            lines.append("")

        body = "\n".join(lines)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-") or "chat"
        return Response(
            body,
            mimetype="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{safe}.md"'},
        )

    # --- Backwards-compatible aliases for the original URL shapes ------------
    @app.get("/get_chats")
    def legacy_get_chats():
        return get_chats()

    @app.get("/search_chats")
    def legacy_search():
        return search_chats()

    @app.get("/get_chat/<int:chat_id>")
    def legacy_get_chat(chat_id: int):
        return get_chat(chat_id)

    @app.post("/chat")
    def legacy_chat():
        return chat()


def register_error_handlers(app: Flask) -> None:

    @app.errorhandler(BadRequest)
    def handle_bad_request(exc: BadRequest):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(404)
    def handle_404(_):
        return jsonify({"error": "Not found."}), 404

    @app.errorhandler(405)
    def handle_405(_):
        return jsonify({"error": "Method not allowed."}), 405

    @app.errorhandler(413)
    def handle_413(_):
        return jsonify({"error": "Request too large."}), 413

    @app.errorhandler(Exception)
    def handle_500(exc: Exception):
        log.exception("unhandled error")
        # Never leak internals to the client.
        return jsonify({"error": "Internal server error."}), 500


app = create_app()

if __name__ == "__main__":
    app.run(debug=config.DEBUG, port=config.PORT, host=os.getenv("HOST", "127.0.0.1"))
