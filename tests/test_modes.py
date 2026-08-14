"""Mode / streaming / continuation tests. Run: python -m tests.test_modes"""
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["ONEAI_SKIP_DOTENV"] = "1"   # ignore the developer's .env

TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "modes.db")
os.environ["OPENROUTER_KEY"] = "sk-or-test"
os.environ["SECRET_KEY"] = "test"
os.environ["STREAMING"] = "true"

import config  # noqa: E402
import llm  # noqa: E402
import models  # noqa: E402
import app as app_module  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  -> ' + detail if detail and not cond else ''}")


def fake_choice(text, finish="stop"):
    return SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish)


class FakeClient:
    """Records calls and replays scripted responses."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, model, messages, timeout=None, max_tokens=None,
                extra_headers=None, stream=False):
        self.calls.append({"model": model, "messages": messages,
                           "max_tokens": max_tokens, "stream": stream})
        result = self.script(model, messages, len(self.calls), stream)
        if isinstance(result, Exception):
            raise result
        return result


def main():
    application = app_module.app
    application.config["TESTING"] = True
    client = application.test_client()

    # ---------- direct mode: one request, not four ----------
    fake = FakeClient(lambda m, msgs, n, s: SimpleNamespace(choices=[fake_choice("direct answer")]))
    llm.build_client = lambda: fake
    models.text_models = lambda: ["vendor/text:free"]
    models.code_models = lambda: ["vendor/coder:free"]

    result = llm.generate_single("hi", [], [], "direct")
    check("direct mode makes exactly one request", len(fake.calls) == 1, str(len(fake.calls)))
    check("direct mode returns the answer", result.answer == "direct answer")
    check("direct mode records its mode", result.mode == "direct")
    check("direct mode records the model", result.model == "vendor/text:free", result.model)

    # ---------- code mode: coding model + big budget ----------
    fake = FakeClient(lambda m, msgs, n, s: SimpleNamespace(choices=[fake_choice("```py\\nx=1\\n```")]))
    llm.build_client = lambda: fake
    result = llm.generate_single("write a script", [], [], "code")
    check("code mode uses a coding model", fake.calls[0]["model"] == "vendor/coder:free",
          fake.calls[0]["model"])
    check("code mode raises the output budget",
          fake.calls[0]["max_tokens"] == config.MAX_TOKENS_CODE,
          str(fake.calls[0]["max_tokens"]))
    check("code mode uses the engineer system prompt",
          "senior software engineer" in fake.calls[0]["messages"][0]["content"],
          fake.calls[0]["messages"][0]["content"][:60])

    # panel budget stays smaller than code budget
    check("code budget exceeds the default", config.MAX_TOKENS_CODE > config.MAX_TOKENS,
          f"{config.MAX_TOKENS_CODE} vs {config.MAX_TOKENS}")

    # ---------- continuation on truncation ----------
    def truncating(model, messages, n, stream):
        if n == 1:
            return SimpleNamespace(choices=[fake_choice("def f():", finish="length")])
        return SimpleNamespace(choices=[fake_choice("    return 1", finish="stop")])

    fake = FakeClient(truncating)
    llm.build_client = lambda: fake
    result = llm.generate_single("long file", [], [], "code")
    check("truncated output triggers a continuation", len(fake.calls) == 2, str(len(fake.calls)))
    check("continuation is stitched onto the answer",
          "def f():" in result.answer and "return 1" in result.answer, result.answer)
    check("continuation resolves the truncated flag", result.truncated is False)
    check("continuation prompt forbids repeating",
          "not repeat" in fake.calls[1]["messages"][-1]["content"].lower(),
          fake.calls[1]["messages"][-1]["content"][:60])

    # continuation is bounded
    fake = FakeClient(lambda m, msgs, n, s: SimpleNamespace(choices=[fake_choice("more", finish="length")]))
    llm.build_client = lambda: fake
    result = llm.generate_single("endless", [], [], "code")
    check("continuations are capped", len(fake.calls) == config.MAX_CONTINUATIONS + 1,
          str(len(fake.calls)))
    check("still-truncated output is flagged", result.truncated is True)

    # ---------- panel still works and still costs four calls ----------
    fake = FakeClient(lambda m, msgs, n, s: SimpleNamespace(choices=[fake_choice("node text")]))
    llm.build_client = lambda: fake
    models.vision_models = lambda: ["vendor/sees:free"]
    result = llm.generate("open question", [])
    check("panel mode makes four requests", len(fake.calls) == 4, str(len(fake.calls)))
    check("panel result reports panel nodes", len(result.nodes) == 3, str(len(result.nodes)))

    # ---------- routes ----------
    captured = {}

    def fake_single(prompt, history=None, attachments=None, mode="direct"):
        captured["mode"] = mode
        return llm.PanelResult(answer="ok", mode=mode, model="vendor/coder:free")

    def fake_panel(prompt, history=None, attachments=None):
        captured["mode"] = "panel"
        return llm.PanelResult(answer="panel ok", nodes={"a": "1"}, mode="panel")

    llm.generate_single = fake_single
    llm.generate = fake_panel

    r = client.post("/api/chat", json={"message": "hi", "mode": "code"})
    body = r.get_json()
    check("code mode routes to the single-model path", captured.get("mode") == "code",
          str(captured))
    check("response reports the mode", body.get("mode") == "code", str(body))
    check("response reports the model", body.get("model") == "vendor/coder:free", str(body))
    chat_id = body["chat_id"]

    client.post("/api/chat", json={"message": "hi", "mode": "panel"})
    check("panel mode still routes to the panel", captured.get("mode") == "panel")

    check("unknown mode rejected",
          client.post("/api/chat", json={"message": "hi", "mode": "wizard"}).status_code == 400)
    check("missing mode falls back to the default",
          client.post("/api/chat", json={"message": "hi"}).status_code == 200)

    # mode persists on the message
    stored = client.get(f"/api/chats/{chat_id}").get_json()
    meta = stored[-1]["meta"]
    check("mode persisted in message meta", meta.get("mode") in ("code", "panel"), str(meta))
    check("model persisted in message meta", "model" in meta, str(meta))

    # ---------- /api/config ----------
    cfg = client.get("/api/config").get_json()
    check("config exposes modes", set(cfg["modes"]) == set(config.MODES), str(cfg))
    check("config exposes streaming flag", cfg["streaming"] is True, str(cfg))

    # ---------- streaming ----------
    def fake_stream(prompt, history=None, attachments=None, mode="direct"):
        yield {"type": "chunk", "text": "def "}
        yield {"type": "chunk", "text": "main():"}
        yield {"type": "done", "model": "vendor/coder:free"}

    llm.stream_single = fake_stream
    r = client.post("/api/chat/stream", json={"message": "write main", "mode": "code"})
    check("stream returns SSE", r.status_code == 200
          and "text/event-stream" in r.headers["Content-Type"], r.headers.get("Content-Type", ""))
    raw = r.get_data(as_text=True)
    check("stream emits a start event", "event: start" in raw, raw[:80])
    check("stream emits chunks", raw.count("event: chunk") == 2, str(raw.count("event: chunk")))
    check("stream emits done", "event: done" in raw)
    check("buffering disabled for proxies", r.headers.get("X-Accel-Buffering") == "no")

    done_line = [l for l in raw.splitlines() if l.startswith("data:") ][-1]
    done = json.loads(done_line[6:])
    stream_chat = done.get("message_id")
    check("streamed answer persisted", isinstance(stream_chat, int), str(done))

    saved = None
    for cid in range(1, 12):
        msgs = client.get(f"/api/chats/{cid}")
        if msgs.status_code == 200:
            for m in msgs.get_json():
                if m["id"] == stream_chat:
                    saved = m
    check("streamed text saved intact", saved and saved["content"] == "def main():",
          str(saved and saved["content"]))

    # panel cannot stream
    check("panel mode refuses to stream",
          client.post("/api/chat/stream", json={"message": "x", "mode": "panel"}).status_code == 400)

    # an empty stream must not be saved as an answer
    def empty_stream(prompt, history=None, attachments=None, mode="direct"):
        yield {"type": "error", "message": "provider down"}

    llm.stream_single = empty_stream
    before = len(client.get(f"/api/chats/{chat_id}").get_json())
    r = client.post("/api/chat/stream", json={"message": "x", "mode": "code",
                                              "chat_id": chat_id})
    raw = r.get_data(as_text=True)
    after = client.get(f"/api/chats/{chat_id}").get_json()
    check("failed stream emits an error event", "event: error" in raw, raw[-120:])
    check("failed stream saves no assistant message",
          len(after) == before + 1 and after[-1]["role"] == "user",
          f"{before} -> {len(after)}, last={after[-1]['role']}")

    # streaming can be turned off
    config.STREAMING = False
    check("streaming disabled returns 409",
          client.post("/api/chat/stream", json={"message": "x", "mode": "code"}).status_code == 409)
    config.STREAMING = True

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failures:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
