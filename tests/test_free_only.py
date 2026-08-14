"""Free-only enforcement tests. Run: python -m tests.test_free_only"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["ONEAI_SKIP_DOTENV"] = "1"   # ignore the developer's .env

TMP = Path(tempfile.mkdtemp())
os.environ["DB_PATH"] = str(TMP / "free.db")
os.environ["OPENROUTER_KEY"] = "sk-or-test"
os.environ["SECRET_KEY"] = "test"

import config  # noqa: E402
import llm  # noqa: E402
import models  # noqa: E402
import app as app_module  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}{'  -> ' + detail if detail and not cond else ''}")


CATALOG = [
    {"id": "cohere/north-mini-code:free", "name": "North Mini Code",
     "description": "agentic coding model for software engineering",
     "pricing": {"prompt": "0", "completion": "0"}, "context_length": 256000,
     "architecture": {"input_modalities": ["text"]}},
    {"id": "nvidia/nemotron-3-super-120b-a12b:free", "name": "Nemotron 3 Super",
     "description": "hybrid MoE for multi-agent applications, SWE-Bench Verified",
     "pricing": {"prompt": "0", "completion": "0"}, "context_length": 1000000,
     "architecture": {"input_modalities": ["text"]}},
    {"id": "poolside/laguna-s-2.1:free", "name": "Laguna S 2.1",
     "description": "coding agent model for agentic coding",
     "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262000,
     "architecture": {"input_modalities": ["text"]}},
    {"id": "google/gemma-4-26b-a4b-it:free", "name": "Gemma 4",
     "description": "multimodal instruction tuned",
     "pricing": {"prompt": "0", "completion": "0"}, "context_length": 262000,
     "architecture": {"input_modalities": ["text", "image"]}},
    # Paid models that must never be selected or called.
    {"id": "anthropic/claude-opus-4.6", "name": "Claude Opus",
     "description": "frontier coding model for software engineering",
     "pricing": {"prompt": "0.000015", "completion": "0.000075"},
     "context_length": 1000000, "architecture": {"input_modalities": ["text", "image"]}},
    {"id": "moonshotai/kimi-k2.7-code", "name": "Kimi K2.7 Code",
     "description": "agentic coding model",
     "pricing": {"prompt": "0.00000095", "completion": "0.000004"},
     "context_length": 262000, "architecture": {"input_modalities": ["text"]}},
]


def use_catalog(entries):
    models._cache.update({"models": entries, "fetched_at": 9e18, "failed_at": 0.0})


def main():
    use_catalog(CATALOG)
    config.FREE_ONLY = True

    # ---------- is_free ----------
    check("free model recognised", models.is_free("cohere/north-mini-code:free"))
    check("paid model rejected", not models.is_free("anthropic/claude-opus-4.6"))
    check("paid coding model rejected", not models.is_free("moonshotai/kimi-k2.7-code"))
    check("auto-router treated as free", models.is_free("openrouter/free"))
    check("empty id rejected", not models.is_free(""))

    # Unknown IDs: conservative fallback on the ':free' convention.
    check("unknown :free id allowed", models.is_free("vendor/brand-new:free"))
    check("unknown paid-looking id rejected", not models.is_free("vendor/brand-new"))

    # ---------- assert_free ----------
    try:
        models.assert_free("anthropic/claude-opus-4.6")
        check("assert_free raises on paid", False, "no exception")
    except models.PaidModelBlocked as exc:
        check("assert_free raises on paid", True)
        check("block message names the model", "claude-opus-4.6" in str(exc), str(exc))
        check("block message explains the fix", "FREE_ONLY" in str(exc), str(exc))

    models.assert_free("cohere/north-mini-code:free")
    check("assert_free permits free", True)

    # ---------- list filtering ----------
    mixed = ["anthropic/claude-opus-4.6", "cohere/north-mini-code:free",
             "moonshotai/kimi-k2.7-code", "openrouter/free"]
    kept = models.free_only(mixed)
    check("free_only strips paid entries",
          kept == ["cohere/north-mini-code:free", "openrouter/free"], str(kept))

    # ---------- code ranking ----------
    config.CODE_MODELS = ["cohere/north-mini-code:free"]
    config.PANEL_MODELS = ["openrouter/free"]
    config.FALLBACK_MODELS = ["openrouter/free"]
    picks = models.code_models()
    check("no paid model in code picks",
          not any(p in ("anthropic/claude-opus-4.6", "moonshotai/kimi-k2.7-code") for p in picks),
          str(picks))
    check("configured coding model comes first",
          picks[0] == "cohere/north-mini-code:free", str(picks[:3]))
    check("coding models discovered from the catalog",
          "poolside/laguna-s-2.1:free" in picks, str(picks))
    check("training-on-free provider ranked last",
          picks.index("poolside/laguna-s-2.1:free") > picks.index("cohere/north-mini-code:free"),
          str(picks))
    check("trains_on_free detects poolside", models.trains_on_free("poolside/laguna-s-2.1:free"))
    check("trains_on_free clears cohere", not models.trains_on_free("cohere/north-mini-code:free"))

    # ---------- vision ----------
    config.VISION_MODELS = ["google/gemma-4-26b-a4b-it:free"]
    vis = models.vision_models()
    check("vision picks are free", all(models.is_free(v) for v in vis), str(vis))
    check("paid vision model excluded", "anthropic/claude-opus-4.6" not in vis, str(vis))

    # ---------- the call site itself blocks ----------
    sent = []

    class Spy:
        @property
        def chat(self):
            return SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, model, messages, **kw):
            sent.append(model)
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="answer"), finish_reason="stop")])

    spy = Spy()
    try:
        llm._complete(spy, ["anthropic/claude-opus-4.6"], [], 10)
        check("paid model never reaches the network", False, "call went through")
    except llm.LLMError:
        check("paid model never reaches the network", len(sent) == 0, str(sent))

    # a paid entry in the list is skipped, the free one still answers
    sent.clear()
    done = llm._complete(spy, ["moonshotai/kimi-k2.7-code", "cohere/north-mini-code:free"], [], 10)
    check("free model still used after skipping paid",
          done.text == "answer" and sent == ["cohere/north-mini-code:free"], str(sent))

    # ---------- FREE_ONLY can be turned off deliberately ----------
    config.FREE_ONLY = False
    sent.clear()
    llm._complete(spy, ["moonshotai/kimi-k2.7-code"], [], 10)
    check("paid model allowed when FREE_ONLY is off", sent == ["moonshotai/kimi-k2.7-code"],
          str(sent))
    config.FREE_ONLY = True

    # ---------- rate limiting is distinguished from failure ----------
    class Limited:
        @property
        def chat(self):
            return SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, model, messages, **kw):
            from openai import APIStatusError
            exc = APIStatusError.__new__(APIStatusError)
            exc.status_code = 429
            exc.args = ("rate limited",)
            raise exc

    try:
        llm._complete(Limited(), ["cohere/north-mini-code:free"], [], 10)
        check("429 raises RateLimited", False, "no exception")
    except llm.RateLimited as exc:
        check("429 raises RateLimited", True)
        check("rate limit message suggests cheaper modes",
              "Direct" in str(exc) and "one request" in str(exc), str(exc))
    except Exception as exc:
        check("429 raises RateLimited", False, f"{type(exc).__name__}: {exc}")

    # ---------- routes ----------
    application = app_module.app
    application.config["TESTING"] = True
    client = application.test_client()

    cfg = client.get("/api/config").get_json()
    check("config reports free_only", cfg["free_only"] is True, str(cfg))
    check("config reports request cost per mode",
          cfg["requests_per_message"]["panel"] == 4
          and cfg["requests_per_message"]["code"] == 1,
          str(cfg.get("requests_per_message")))

    listing = client.get("/api/models").get_json()
    check("models endpoint lists code picks", len(listing["code"]) > 0, str(listing)[:120])
    check("models endpoint marks everything free",
          all(m["free"] for m in listing["code"] + listing["text"] + listing["vision"]),
          str(listing)[:160])
    check("models endpoint flags training providers",
          any("trains_on_free" in m for m in listing["code"]))

    # rate limit surfaces as 429 to the client
    def limited(prompt, history=None, attachments=None, mode="direct"):
        raise llm.RateLimited("Free-tier rate limit reached. Use Direct mode.")

    llm.generate_single = limited
    r = client.post("/api/chat", json={"message": "hi", "mode": "code"})
    check("rate limit returns 429 to the client", r.status_code == 429, str(r.status_code))
    check("rate limit message reaches the client",
          "rate limit" in r.get_json()["error"].lower(), str(r.get_json()))

    # ---------- catalog unreachable: still refuses paid-looking IDs ----------
    use_catalog([])
    models._cache["failed_at"] = 9e18  # suppress refetch
    check("offline: :free id allowed", models.is_free("vendor/x:free"))
    check("offline: bare id refused", not models.is_free("vendor/x"))
    check("offline: known paid id refused", not models.is_free("anthropic/claude-opus-4.6"))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failures:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
