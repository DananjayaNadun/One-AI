"""Live model discovery.

OpenRouter's free roster churns weekly -- models get delisted or repriced with
no notice, and a hardcoded ID that worked last month returns a 404 today. Rather
than pinning IDs, we query the catalog at startup, cache it, and pick models by
capability (free, vision-capable, big enough context).

If the catalog is unreachable we fall back to whatever is configured in .env, so
the app still starts on a restricted network.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

import config

log = logging.getLogger(__name__)

_CATALOG_URL = "https://openrouter.ai/api/v1/models"
_lock = threading.Lock()
_cache: dict = {"fetched_at": 0.0, "models": [], "failed_at": 0.0}


def _fetch_catalog() -> list[dict]:
    req = urllib.request.Request(
        _CATALOG_URL,
        headers={"Accept": "application/json", "User-Agent": "one-ai/1.0"},
    )
    with urllib.request.urlopen(req, timeout=config.CATALOG_TIMEOUT_SECONDS) as res:
        payload = json.loads(res.read().decode("utf-8"))
    return payload.get("data", []) or []


def _is_free(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        return (
            float(pricing.get("prompt", 1)) == 0.0
            and float(pricing.get("completion", 1)) == 0.0
        )
    except (TypeError, ValueError):
        return False


def _supports_images(model: dict) -> bool:
    arch = model.get("architecture") or {}
    modalities = arch.get("input_modalities") or []
    if modalities:
        return "image" in modalities
    # Older catalog entries expose a single "text+image->text" string instead.
    return "image" in str(arch.get("modality", "")).lower()


def get_catalog(force: bool = False) -> list[dict]:
    """Return the cached model catalog, refreshing if stale."""
    with _lock:
        now = time.time()
        if not force and _cache["models"] and (now - _cache["fetched_at"]) < config.CATALOG_TTL_SECONDS:
            return _cache["models"]

        # Negative caching. Without this, a network that cannot reach
        # openrouter.ai (a free PythonAnywhere account, for one) would attempt
        # a fetch on every single request and add the full timeout to each.
        if not force and (now - _cache["failed_at"]) < config.CATALOG_RETRY_SECONDS:
            return _cache["models"]

        try:
            models = _fetch_catalog()
            _cache.update({"models": models, "fetched_at": now, "failed_at": 0.0})
            log.info("model catalog refreshed: %d models", len(models))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            _cache["failed_at"] = now
            log.warning("could not refresh model catalog (%s); using configured IDs", exc)
        return _cache["models"]


class PaidModelBlocked(RuntimeError):
    """Raised when something tried to call a model that is not free."""


def _lookup(model_id: str) -> dict | None:
    for model in get_catalog():
        if model.get("id") == model_id:
            return model
    return None


def is_free(model_id: str) -> bool:
    """True only if we can positively establish the model costs nothing.

    Verified against live catalog pricing when the catalog is reachable. When it
    is not, we fall back to OpenRouter's ':free' naming convention, which is
    conservative: an unknown ID without the suffix is treated as paid.
    """
    if not model_id:
        return False
    if model_id == "openrouter/free":
        return True  # the auto-router only selects free models

    entry = _lookup(model_id)
    if entry is not None:
        return _is_free(entry)
    return model_id.endswith(":free")


def assert_free(model_id: str) -> None:
    if config.FREE_ONLY and not is_free(model_id):
        raise PaidModelBlocked(
            f"'{model_id}' is not a free model. FREE_ONLY is on, so the request "
            "was blocked before it could bill your account. Remove it from your "
            ".env, or set FREE_ONLY=false if you meant to pay."
        )


def free_only(model_ids: list[str]) -> list[str]:
    """Drop anything that is not verifiably free, preserving order."""
    if not config.FREE_ONLY:
        return list(model_ids)
    kept, dropped = [], []
    for model_id in model_ids:
        (kept if is_free(model_id) else dropped).append(model_id)
    if dropped:
        log.warning("blocked non-free model(s): %s", ", ".join(dropped))
    return kept


def trains_on_free(model_id: str) -> bool:
    """Whether this provider states it may train on free-tier traffic."""
    vendor = str(model_id).split("/", 1)[0].lower()
    return vendor in {v.lower() for v in config.TRAINS_ON_FREE}


def _rank(model: dict) -> tuple:
    """Prefer larger context, then a stable name for determinism."""
    return (-int(model.get("context_length") or 0), model.get("id", ""))


def discover(free_only: bool = True, vision: bool = False, limit: int = 6) -> list[str]:
    """Return model IDs matching the requested capabilities, best first."""
    catalog = get_catalog()
    if not catalog:
        return []

    picks = []
    for model in catalog:
        model_id = model.get("id")
        if not model_id:
            continue
        if free_only and not _is_free(model):
            continue
        if vision and not _supports_images(model):
            continue
        # The auto-router is not a concrete model; it is handled separately.
        if model_id == "openrouter/free":
            continue
        picks.append(model)

    picks.sort(key=_rank)
    return [m["id"] for m in picks[:limit]]


def text_models() -> list[str]:
    """Panel/aggregator models: configured IDs first, then live discovery."""
    discovered = discover(free_only=True, vision=False)
    ordered = [*config.PANEL_MODELS, *discovered, *config.FALLBACK_MODELS]
    return free_only(_dedupe(ordered))


def code_models() -> list[str]:
    """Coding-tuned models first, then anything else free and long-context."""
    configured = [m for m in config.CODE_MODELS if m]
    discovered = [
        mid for mid in discover(free_only=True, limit=10)
        if any(tag in mid.lower() for tag in ("cod", "qwen", "laguna", "devstral", "starcoder"))
    ]
    return free_only(_dedupe([*configured, *discovered, *text_models()]))


def vision_models() -> list[str]:
    """Models that can actually accept an image."""
    configured = [m for m in config.VISION_MODELS if m]
    discovered = discover(free_only=True, vision=True)
    return free_only(_dedupe([*configured, *discovered]))


def _dedupe(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out
