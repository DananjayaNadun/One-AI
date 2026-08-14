"""Centralised configuration. Everything tunable lives here, read from env."""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# load_dotenv is a no-op if .env is missing, which is what we want in prod
# where real environment variables are set by the host.
#
# Tests set ONEAI_SKIP_DOTENV so a developer's own .env cannot change what the
# suite exercises. Without this, setting DEFAULT_MODE=code in .env silently
# reroutes requests and makes unrelated tests fail.
if os.getenv("ONEAI_SKIP_DOTENV") != "1":
    load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name) or default
    return [item.strip() for item in raw.split(",") if item.strip()]


# --- Secrets -----------------------------------------------------------------
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY", "").strip()
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()

# --- Runtime -----------------------------------------------------------------
DEBUG = _bool("FLASK_DEBUG", False)
PORT = _int("PORT", 5000)

# Absolute path matters on PythonAnywhere: the working directory of the WSGI
# worker is not the project directory, so a relative 'database.db' silently
# creates a second, empty database somewhere else.
DB_PATH = Path(os.getenv("DB_PATH") or (BASE_DIR / "database.db")).resolve()

# --- Model routing -----------------------------------------------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Free model IDs rotate on OpenRouter without notice, so these are configurable
# and the client falls through the list until one responds.
# FREE ONLY. Nothing in this app may call a paid model. The guard in models.py
# verifies every ID against live catalog pricing before it is used, so a typo or
# a stale copy-paste cannot quietly start charging your account.
FREE_ONLY = _bool("FREE_ONLY", True)

# The free roster is delisted and reshuffled weekly -- IDs pinned here are only
# a starting point. models.py queries the live catalog and appends whatever is
# actually available today, so the app keeps working when these disappear.
PANEL_MODELS = _csv("PANEL_MODELS", "openrouter/free")
AGGREGATOR_MODEL = os.getenv("AGGREGATOR_MODEL", "openrouter/free").strip()
FALLBACK_MODELS = _csv("FALLBACK_MODELS", "openrouter/free")

# --- Modes -------------------------------------------------------------------
# "panel"  : three personas + an aggregator. Good for open questions.
# "direct" : one model, one request. 4x fewer requests, 4x faster.
# "code"   : one coding-tuned model, big output budget, terse system prompt.
#
# The panel is the wrong tool for code. Three weak models produce three
# different designs and a fourth weak model, which never runs any of them,
# splices them together. Code is verifiable and brittle; ensembling helps when
# errors are uncorrelated and answers are fuzzy, which is the opposite case.
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "panel").strip() or "panel"
MODES = ("panel", "direct", "code")

# Coding-tuned free models, best first. Discovery appends whatever else is live.
# Ranked by OpenRouter's own Programming category, best first. Poolside models
# are deliberately last: their free tier states that inputs and outputs may be
# used to train their models, which is a poor trade when the input is your code.
CODE_MODELS = _csv(
    "CODE_MODELS",
    "cohere/north-mini-code:free,"
    "nvidia/nemotron-3.5-lightning:free,"
    "nvidia/nemotron-3-super-120b-a12b:free,"
    "openai/gpt-oss-20b:free",
)

# Providers known to train on free-tier traffic. Used only to warn, never to
# block -- it is your call, but it should be an informed one.
TRAINS_ON_FREE = _csv("TRAINS_ON_FREE", "poolside")

# 2048 tokens is roughly 150 lines of code -- long answers were being truncated
# mid-function with no indication. Code mode gets a far larger budget.
MAX_TOKENS_CODE = _int("MAX_TOKENS_CODE", 32_000)

# When a response stops because it hit the ceiling, ask the model to continue
# from where it stopped rather than handing back half a file.
MAX_CONTINUATIONS = _int("MAX_CONTINUATIONS", 2)

# Streaming is only possible for single-model modes. It is disabled by default
# because PythonAnywhere buffers responses, which makes a stream arrive as one
# lump at the end -- slower than not streaming. Turn it on when running locally.
STREAMING = _bool("STREAMING", False)

PANEL_TIMEOUT_SECONDS = _int("PANEL_TIMEOUT_SECONDS", 45)
AGGREGATOR_TIMEOUT_SECONDS = _int("AGGREGATOR_TIMEOUT_SECONDS", 90)
MAX_TOKENS = _int("MAX_TOKENS", 4096)

# Vision-capable free models, for image attachments. Leave blank to rely purely
# on live discovery from the OpenRouter catalog.
VISION_MODELS = _csv(
    "VISION_MODELS",
    "google/gemma-4-26b-a4b-it:free,"
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free,"
    "nvidia/nemotron-nano-12b-v2-vl:free",
)

# Live catalog discovery. The free roster changes weekly, so we look models up
# rather than trusting IDs baked in at build time.
CATALOG_TTL_SECONDS = _int("CATALOG_TTL_SECONDS", 6 * 60 * 60)
CATALOG_TIMEOUT_SECONDS = _int("CATALOG_TIMEOUT_SECONDS", 10)
CATALOG_RETRY_SECONDS = _int("CATALOG_RETRY_SECONDS", 300)

# --- Attachments -------------------------------------------------------------
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_MB", 12) * 1024 * 1024
MAX_FILES_PER_MESSAGE = _int("MAX_FILES_PER_MESSAGE", 5)
MAX_EXTRACTED_CHARS = _int("MAX_EXTRACTED_CHARS", 60_000)
MAX_PDF_PAGES = _int("MAX_PDF_PAGES", 60)
MAX_IMAGE_DIMENSION = _int("MAX_IMAGE_DIMENSION", 1536)
MAX_IMAGE_BYTES = _int("MAX_IMAGE_KB", 900) * 1024
ATTACHMENT_TTL_SECONDS = _int("ATTACHMENT_TTL_SECONDS", 24 * 60 * 60)

# --- Free-tier request budget ------------------------------------------------
# OpenRouter's free tier is roughly 20 requests/minute and a low daily cap.
# Panel mode spends four requests per message, so the daily cap arrives four
# times faster than people expect. These numbers drive the in-app counter.
FREE_RPM = _int("FREE_RPM", 20)
FREE_RPD = _int("FREE_RPD", 200)

# --- Limits ------------------------------------------------------------------
MAX_PROMPT_CHARS = _int("MAX_PROMPT_CHARS", 12_000)
MAX_TITLE_CHARS = _int("MAX_TITLE_CHARS", 120)
HISTORY_TURNS = _int("HISTORY_TURNS", 12)  # messages replayed to the model

# --- CORS --------------------------------------------------------------------
# Same-origin app: no cross-origin access needed by default. Set CORS_ORIGINS
# only if you actually call this API from another domain.
CORS_ORIGINS = _csv("CORS_ORIGINS", "")


class ConfigError(RuntimeError):
    pass


def validate() -> list[str]:
    """Return a list of warnings; raise on anything fatal."""
    warnings: list[str] = []

    if not OPENROUTER_KEY:
        raise ConfigError(
            "OPENROUTER_KEY is not set. Copy .env.example to .env and fill it in."
        )
    if not OPENROUTER_KEY.startswith("sk-or-"):
        warnings.append(
            "OPENROUTER_KEY does not look like an OpenRouter key (expected 'sk-or-...')."
        )
    if not SECRET_KEY:
        warnings.append("SECRET_KEY is not set; using an ephemeral random key.")
    if DEBUG:
        warnings.append("FLASK_DEBUG is on. Never enable this on a public host.")
    return warnings
