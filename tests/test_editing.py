"""Edit / regenerate / export tests. Run: python -m tests.test_editing"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["ONEAI_SKIP_DOTENV"] = "1"   # ignore the developer's .env
os.environ["DEFAULT_MODE"] = "panel"    # these suites exercise the panel path

TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "edit.db")
os.environ["OPENROUTER_KEY"] = "sk-or-test"
os.environ["SECRET_KEY"] = "test"

import db  # noqa: E402
import llm  # noqa: E402
import app as app_module  # noqa: E402

PASS, FAIL = [], []
calls = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  -> ' + detail if detail and not cond else ''}")


def main():
    counter = {"n": 0}

    def fake_generate(prompt, history=None, attachments=None):
        counter["n"] += 1
        calls.append({"prompt": prompt, "history": list(history or [])})
        return llm.PanelResult(answer=f"Answer #{counter['n']} to: {prompt[:30]}",
                               nodes={"logic": "a"})

    llm.generate = fake_generate
    application = app_module.app
    application.config["TESTING"] = True
    client = application.test_client()

    # --- build a two-turn conversation ---
    r = client.post("/api/chat", json={"message": "First question", "chat_id": None})
    body = r.get_json()
    chat_id = body["chat_id"]
    check("chat returns assistant message_id", isinstance(body["message_id"], int), str(body))
    check("chat returns user_message_id", isinstance(body["user_message_id"], int))
    first_assistant = body["message_id"]

    client.post("/api/chat", json={"message": "Second question", "chat_id": chat_id})
    msgs = client.get(f"/api/chats/{chat_id}").get_json()
    check("four messages after two turns", len(msgs) == 4, str(len(msgs)))

    # --- THE BUG: regenerate must replace, not append ---
    last_assistant = msgs[-1]["id"]
    before = len(msgs)
    r = client.post(f"/api/chats/{chat_id}/regenerate", json={"message_id": last_assistant})
    check("regenerate succeeds", r.status_code == 200, str(r.get_json()))
    after = client.get(f"/api/chats/{chat_id}").get_json()
    check("regenerate does NOT duplicate the exchange", len(after) == before,
          f"{before} -> {len(after)}")
    check("regenerate replaced the answer text",
          after[-1]["content"] != msgs[-1]["content"], after[-1]["content"])
    check("regenerate returns the new message id",
          r.get_json()["message_id"] == after[-1]["id"])
    check("regenerated from the right prompt",
          calls[-1]["prompt"] == "Second question", calls[-1]["prompt"])
    check("regenerate history excludes the turn being redone",
          all(c["content"] != "Second question" for c in calls[-1]["history"]),
          str(calls[-1]["history"]))

    # regenerate an EARLIER answer: everything after it must go
    r = client.post(f"/api/chats/{chat_id}/regenerate", json={"message_id": first_assistant})
    check("regenerating an earlier answer succeeds", r.status_code == 200, str(r.get_json()))
    after = client.get(f"/api/chats/{chat_id}").get_json()
    check("later turns truncated on earlier regenerate", len(after) == 2, str(len(after)))
    check("truncation kept the original user turn", after[0]["content"] == "First question")

    # --- validation ---
    check("regenerate rejects a user message",
          client.post(f"/api/chats/{chat_id}/regenerate",
                      json={"message_id": after[0]["id"]}).status_code == 400)
    check("regenerate 404s on unknown message",
          client.post(f"/api/chats/{chat_id}/regenerate",
                      json={"message_id": 999999}).status_code == 404)
    check("regenerate rejects non-integer id",
          client.post(f"/api/chats/{chat_id}/regenerate",
                      json={"message_id": "abc"}).status_code == 400)

    # message belonging to a different chat must not be reachable
    other = client.post("/api/chat", json={"message": "Other chat", "chat_id": None}).get_json()
    check("regenerate rejects cross-chat message id",
          client.post(f"/api/chats/{chat_id}/regenerate",
                      json={"message_id": other["message_id"]}).status_code == 404)

    # --- edit ---
    convo = client.get(f"/api/chats/{chat_id}").get_json()
    user_id = convo[0]["id"]
    r = client.post(f"/api/chats/{chat_id}/edit",
                    json={"message_id": user_id, "message": "Rewritten question"})
    check("edit succeeds", r.status_code == 200, str(r.get_json()))
    after = client.get(f"/api/chats/{chat_id}").get_json()
    check("edit replaces rather than appends", len(after) == 2, str(len(after)))
    check("edited text saved", after[0]["content"] == "Rewritten question", after[0]["content"])
    check("edit produced a fresh answer", after[1]["role"] == "assistant")
    check("edit re-answered the new text",
          calls[-1]["prompt"] == "Rewritten question", calls[-1]["prompt"])

    check("edit rejects an assistant message",
          client.post(f"/api/chats/{chat_id}/edit",
                      json={"message_id": after[1]["id"], "message": "x"}).status_code == 400)
    check("edit rejects empty text",
          client.post(f"/api/chats/{chat_id}/edit",
                      json={"message_id": after[0]["id"], "message": "  "}).status_code == 400)

    # edit in the middle of a longer chat truncates the tail
    client.post("/api/chat", json={"message": "Turn two", "chat_id": chat_id})
    client.post("/api/chat", json={"message": "Turn three", "chat_id": chat_id})
    convo = client.get(f"/api/chats/{chat_id}").get_json()
    check("six messages before mid-edit", len(convo) == 6, str(len(convo)))
    mid = convo[2]["id"]
    client.post(f"/api/chats/{chat_id}/edit", json={"message_id": mid, "message": "Turn two v2"})
    convo = client.get(f"/api/chats/{chat_id}").get_json()
    check("mid-conversation edit truncates the tail", len(convo) == 4, str(len(convo)))
    check("mid-edit preserved earlier turns", convo[0]["content"] == "Rewritten question")

    # --- failure must not truncate ---
    def boom(prompt, history=None, attachments=None):
        raise llm.LLMError("down")

    llm.generate = boom
    snapshot = client.get(f"/api/chats/{chat_id}").get_json()
    r = client.post(f"/api/chats/{chat_id}/regenerate",
                    json={"message_id": snapshot[-1]["id"]})
    check("regenerate failure returns 503", r.status_code == 503, str(r.status_code))
    restored = client.get(f"/api/chats/{chat_id}").get_json()
    check("failed regenerate leaves history intact",
          len(restored) == len(snapshot) and restored[-1]["content"] == snapshot[-1]["content"],
          f"{len(snapshot)} -> {len(restored)}")

    r = client.post(f"/api/chats/{chat_id}/edit",
                    json={"message_id": restored[0]["id"], "message": "should not apply"})
    check("edit failure returns 503", r.status_code == 503)
    after_fail = client.get(f"/api/chats/{chat_id}").get_json()
    check("failed edit does not destroy the message",
          after_fail[0]["content"] == restored[0]["content"], after_fail[0]["content"])
    llm.generate = fake_generate

    # --- consensus metadata persists ---
    fresh = client.post("/api/chat", json={"message": "meta check", "chat_id": None}).get_json()
    check("chat response carries nodes", isinstance(fresh.get("nodes"), list), str(fresh))
    check("chat response carries seconds", isinstance(fresh.get("seconds"), int), str(fresh))
    stored = client.get(f"/api/chats/{fresh['chat_id']}").get_json()
    meta = stored[-1].get("meta") or {}
    check("assistant meta persisted", meta.get("nodes") == ["logic"], str(meta))
    check("elapsed seconds persisted", isinstance(meta.get("seconds"), int) and meta["seconds"] >= 1,
          str(meta))
    check("degraded flag persisted", meta.get("degraded") is False, str(meta))
    check("user message has empty meta", (stored[0].get("meta") or {}) == {}, str(stored[0].get("meta")))

    # --- export ---
    r = client.get(f"/api/chats/{chat_id}/export")
    check("export returns markdown", r.status_code == 200
          and "markdown" in r.headers["Content-Type"], r.headers.get("Content-Type", ""))
    text = r.get_data(as_text=True)
    check("export contains the conversation", "Rewritten question" in text, text[:120])
    check("export has role headings", "## You" in text and "## One AI" in text)
    check("export sets a download filename",
          "attachment" in r.headers.get("Content-Disposition", ""),
          r.headers.get("Content-Disposition", ""))
    check("export 404s on unknown chat",
          client.get("/api/chats/99999/export").status_code == 404)

    # filename must be sanitised, not echoed
    weird = client.post("/api/chat", json={"message": 'a/b "c" <script>', "chat_id": None}).get_json()
    r = client.get(f"/api/chats/{weird['chat_id']}/export")
    disposition = r.headers.get("Content-Disposition", "")
    check("export filename sanitised",
          '"' not in disposition.split("filename=")[1][1:-1] and "/" not in disposition.split("filename=")[1],
          disposition)

    # --- truncate_from is chat-scoped ---
    a = client.post("/api/chat", json={"message": "chat A", "chat_id": None}).get_json()
    b = client.post("/api/chat", json={"message": "chat B", "chat_id": None}).get_json()
    db.truncate_from(a["chat_id"], a["user_message_id"])
    check("truncate_from does not touch other chats",
          len(client.get(f"/api/chats/{b['chat_id']}").get_json()) == 2)
    check("truncate_from cleared the target chat",
          len(client.get(f"/api/chats/{a['chat_id']}").get_json()) == 0)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failures:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
