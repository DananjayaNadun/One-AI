"""Expert-panel orchestration against OpenRouter.

Flow: three persona calls run in parallel, then an aggregator model reconciles
them into one answer. If every panel node fails we fall straight through to a
single direct answer rather than returning an error string dressed up as a
reply -- the original code returned the exception text as the assistant message
and then saved it into the chat history as if it were a real answer.
"""
from __future__ import annotations

import concurrent.futures
import logging
from dataclasses import dataclass, field
from datetime import datetime

from openai import APIStatusError, APITimeoutError, OpenAI

import config
import models as model_catalog

log = logging.getLogger(__name__)

# OpenRouter asks for these so your app shows up in its dashboards.
_EXTRA_HEADERS = {
    "HTTP-Referer": "https://one-ai.local",
    "X-Title": "One AI",
}

PERSONAS: dict[str, str] = {
    "logic": (
        "You are the reasoning node of an expert panel. Work the problem "
        "step by step, state your assumptions explicitly, and flag anything "
        "you are uncertain about. Be concise."
    ),
    "technical": (
        "You are the technical node of an expert panel. Focus on correctness, "
        "architecture, edge cases, and working code. If code is not relevant, "
        "give precise technical detail instead. Be concise."
    ),
    "clarity": (
        "You are the communication node of an expert panel. Give a clear, "
        "well-structured, readable answer for a general audience. Be concise."
    ),
}


class LLMError(RuntimeError):
    """Raised when no model could produce an answer."""


class RateLimited(LLMError):
    """The free tier's request cap was hit. Distinct from a model failure."""


def build_user_content(prompt: str, attachments: list[dict] | None):
    """Turn a prompt plus attachments into OpenAI-format message content.

    Text and documents are inlined as text parts. Images become image_url parts
    with a data URL, which is what every vision model on OpenRouter expects.
    Returns a plain string when there is nothing but text, because some
    text-only models reject the list form.
    """
    attachments = attachments or []
    if not attachments:
        return prompt

    parts: list[dict] = []
    inlined: list[str] = []

    for att in attachments:
        if att.get("kind") == "image":
            if att.get("data_url"):
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": att["data_url"]},
                })
        elif att.get("text"):
            note = " (truncated)" if att.get("truncated") else ""
            inlined.append(
                f"--- Attached file: {att.get('name', 'file')}{note} ---\n{att['text']}"
            )

    text_block = prompt
    if inlined:
        text_block = f"{prompt}\n\n" + "\n\n".join(inlined)

    if not parts:
        return text_block

    image_names = [a.get("name", "image") for a in attachments if a.get("kind") == "image"]
    if image_names:
        text_block += f"\n\n(Attached image(s): {', '.join(image_names)})"

    # Text first, then images: several vision models weight the leading text
    # part as the instruction.
    return [{"type": "text", "text": text_block}, *parts]


def has_images(attachments: list[dict] | None) -> bool:
    return any(a.get("kind") == "image" for a in (attachments or []))


@dataclass
class Completion:
    text: str
    model: str = ""
    truncated: bool = False


CODE_SYSTEM = (
    "You are a senior software engineer. Answer with working code and only the "
    "explanation the reader actually needs. State assumptions in one line, flag "
    "anything that will break, and never leave a snippet half-finished. Always "
    "label code blocks with the language, and give a filename when it matters."
)

DIRECT_SYSTEM = "You are One AI, a careful and concise assistant. Today is {today}."


@dataclass
class PanelResult:
    answer: str
    nodes: dict[str, str] = field(default_factory=dict)
    degraded: bool = False
    seconds: float = 0.0
    mode: str = "panel"
    model: str = ""
    truncated: bool = False


def build_client() -> OpenAI:
    return OpenAI(
        api_key=config.OPENROUTER_KEY,
        base_url=config.OPENROUTER_BASE_URL,
        timeout=config.AGGREGATOR_TIMEOUT_SECONDS,
        max_retries=1,
    )


def _complete(
    client: OpenAI,
    models: list[str],
    messages: list[dict[str, str]],
    timeout: int,
    max_tokens: int | None = None,
) -> "Completion":
    """Try each model in order; return the first successful completion."""
    last_error: Exception | None = None

    for model in models:
        if not model:
            continue
        try:
            # Last line of defence: even if a paid ID reached this list, it is
            # blocked here, before the request is sent.
            model_catalog.assert_free(model)
            res = client.chat.completions.create(
                model=model,
                messages=messages,
                timeout=timeout,
                max_tokens=max_tokens or config.MAX_TOKENS,
                extra_headers=_EXTRA_HEADERS,
            )
            # A 200 response can still carry an upstream error payload on
            # OpenRouter, in which case choices is empty.
            if not res.choices:
                raise LLMError(f"{model} returned no choices")
            choice = res.choices[0]
            content = (choice.message.content or "").strip()
            if not content:
                raise LLMError(f"{model} returned empty content")
            return Completion(
                text=content,
                model=model,
                truncated=(getattr(choice, "finish_reason", None) == "length"),
            )
        except model_catalog.PaidModelBlocked as exc:
            log.error("%s", exc)
            last_error = exc
        except APIStatusError as exc:
            # 429 is the free tier's daily or per-minute cap, not a broken model.
            if getattr(exc, "status_code", None) == 429:
                raise RateLimited(
                    "Free-tier rate limit reached. OpenRouter allows about "
                    f"{config.FREE_RPM} requests per minute and a limited number "
                    "per day. Wait a minute, or switch to Direct or Code mode, "
                    "which use one request per message instead of four."
                ) from exc
            log.warning("model %s failed: %s", model, exc)
            last_error = exc
        except (APITimeoutError, LLMError) as exc:
            log.warning("model %s failed: %s", model, exc)
            last_error = exc
        except Exception as exc:  # network errors, SDK errors
            log.warning("model %s failed: %s: %s", model, type(exc).__name__, exc)
            last_error = exc

    raise LLMError(str(last_error) if last_error else "no models configured")


def _aggregator_turn(user_content, panel_block: str) -> dict:
    """Re-attach any images to the aggregator turn.

    If we sent only the text, the aggregator would be reconciling notes about
    an image it cannot see.
    """
    note = f"\n\n--- INTERNAL PANEL NOTES (do not quote) ---\n{panel_block}"
    if isinstance(user_content, str):
        return {"role": "user", "content": user_content + note}

    parts = [dict(p) for p in user_content]
    for part in parts:
        if part.get("type") == "text":
            part["text"] = part["text"] + note
            break
    else:
        parts.insert(0, {"type": "text", "text": note})
    return {"role": "user", "content": parts}


def _ask_panel_node(
    client: OpenAI,
    persona: str,
    history: list[dict[str, str]],
    user_content,
    model: str,
) -> str:
    messages = [{"role": "system", "content": persona}, *history, {"role": "user", "content": user_content}]
    return _complete(client, [model, *config.FALLBACK_MODELS], messages,
                     config.PANEL_TIMEOUT_SECONDS).text





def generate(
    prompt: str,
    history: list[dict[str, str]] | None = None,
    attachments: list[dict] | None = None,
) -> PanelResult:
    """Run the panel and return the synthesised answer.

    `history` is prior turns of this conversation, as OpenAI-format messages.
    The original implementation never passed history, so the assistant had no
    memory within a chat -- every turn was answered cold.
    """
    client = build_client()
    history = history or []
    attachments = attachments or []
    today = datetime.now().strftime("%A, %d %B %Y")

    user_content = build_user_content(prompt, attachments)
    vision = has_images(attachments)

    if vision:
        # Only a vision-capable model can see the image. Sending it to a
        # text-only model silently drops the image and produces a confident
        # answer about nothing, which is worse than an error.
        candidates = model_catalog.vision_models()
        if not candidates:
            raise LLMError(
                "No vision-capable model is available right now. Set VISION_MODELS "
                "in .env to a current free model that accepts images."
            )
        panel_pool = candidates
        aggregator_models = candidates
    else:
        panel_pool = model_catalog.text_models() or [config.AGGREGATOR_MODEL]
        aggregator_models = [config.AGGREGATOR_MODEL, *model_catalog.text_models()]

    nodes: dict[str, str] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(PERSONAS)) as pool:
        futures = {
            pool.submit(
                _ask_panel_node,
                client,
                instruction,
                history,
                user_content,
                panel_pool[i % len(panel_pool)],
            ): name
            for i, (name, instruction) in enumerate(PERSONAS.items())
        }
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                nodes[name] = future.result()
            except Exception as exc:
                log.warning("panel node %s unavailable: %s", name, exc)

    if not nodes:
        # Panel is fully down. Answer directly rather than surfacing an error.
        log.warning("all panel nodes failed; falling back to a direct answer")
        messages = [
            {"role": "system", "content": f"You are One AI, a helpful assistant. Today is {today}."},
            *history,
            {"role": "user", "content": user_content},
        ]
        done = _complete(client, aggregator_models, messages, config.AGGREGATOR_TIMEOUT_SECONDS)
        return PanelResult(answer=done.text, nodes={}, degraded=True, model=done.model)

    panel_block = "\n\n".join(
        f"[{name.upper()} NODE]\n{text}" for name, text in nodes.items()
    )
    system = (
        f"You are One AI's master aggregator. Today is {today}. "
        "Several expert nodes have independently answered the user's question. "
        "Reconcile any factual conflicts, discard weak or repeated reasoning, "
        "and write one unified answer in your own voice. Never mention the "
        "panel, the nodes, or that multiple models were consulted."
    )
    messages = [
        {"role": "system", "content": system},
        *history,
        _aggregator_turn(user_content, panel_block),
    ]

    done = _complete(client, aggregator_models, messages, config.AGGREGATOR_TIMEOUT_SECONDS)
    return PanelResult(answer=done.text, nodes=nodes, model=done.model,
                       degraded=len(nodes) < len(PERSONAS))


# --- Single-model modes ------------------------------------------------------

def _mode_setup(mode: str) -> tuple[str, list[str], int]:
    """System prompt, candidate models, and output budget for a mode."""
    today = datetime.now().strftime("%A, %d %B %Y")
    if mode == "code":
        return (
            f"{CODE_SYSTEM}\nToday is {today}.",
            model_catalog.code_models(),
            config.MAX_TOKENS_CODE,
        )
    return (
        DIRECT_SYSTEM.format(today=today),
        model_catalog.text_models() or [config.AGGREGATOR_MODEL],
        config.MAX_TOKENS,
    )


def generate_single(
    prompt: str,
    history: list[dict[str, str]] | None = None,
    attachments: list[dict] | None = None,
    mode: str = "direct",
) -> PanelResult:
    """One model, one request.

    Four sequential calls per message is the wrong trade for code: it burns the
    free-tier request budget four times as fast and adds a minute of latency to
    reach an answer that a single coding model produces better.
    """
    client = build_client()
    history = history or []
    attachments = attachments or []
    user_content = build_user_content(prompt, attachments)

    system, candidates, budget = _mode_setup(mode)
    if has_images(attachments):
        candidates = model_catalog.vision_models() or candidates
        if not candidates:
            raise LLMError("No vision-capable model is available for image input.")

    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_content},
    ]

    done = _complete(client, candidates, messages,
                     config.AGGREGATOR_TIMEOUT_SECONDS, budget)
    text, truncated = done.text, done.truncated

    # Continue where it stopped rather than returning half a file.
    continuations = 0
    while truncated and continuations < config.MAX_CONTINUATIONS:
        continuations += 1
        log.info("output hit the token ceiling; continuing (%d)", continuations)
        follow = [
            *messages,
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                "Continue from exactly where you stopped. Do not repeat any "
                "previous text, do not re-open a code fence you already opened, "
                "and do not add a preamble."},
        ]
        try:
            more = _complete(client, [done.model, *candidates], follow,
                             config.AGGREGATOR_TIMEOUT_SECONDS, budget)
        except LLMError:
            break
        text = text.rstrip() + more.text.lstrip()
        truncated = more.truncated

    return PanelResult(
        answer=text,
        nodes={},
        degraded=False,
        mode=mode,
        model=done.model,
        truncated=truncated,
    )


def stream_single(
    prompt: str,
    history: list[dict[str, str]] | None = None,
    attachments: list[dict] | None = None,
    mode: str = "direct",
):
    """Yield text chunks as they arrive. Single-model modes only.

    The panel cannot stream meaningfully: nothing useful exists until the
    aggregator runs, and by then the answer is complete anyway.
    """
    client = build_client()
    history = history or []
    attachments = attachments or []
    user_content = build_user_content(prompt, attachments)

    system, candidates, budget = _mode_setup(mode)
    if has_images(attachments):
        candidates = model_catalog.vision_models() or candidates

    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": user_content},
    ]

    last_error: Exception | None = None
    for model in candidates:
        try:
            model_catalog.assert_free(model)
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                max_tokens=budget,
                timeout=config.AGGREGATOR_TIMEOUT_SECONDS,
                extra_headers=_EXTRA_HEADERS,
            )
            produced = False
            for chunk in stream:
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content or ""
                if piece:
                    produced = True
                    yield {"type": "chunk", "text": piece}
            if not produced:
                raise LLMError(f"{model} streamed nothing")
            yield {"type": "done", "model": model}
            return
        except Exception as exc:
            # Only fall through to the next model if nothing was emitted yet;
            # otherwise the client would receive two overlapping answers.
            log.warning("stream failed on %s: %s", model, exc)
            last_error = exc
            if locals().get("produced"):
                yield {"type": "error", "message": "The connection dropped mid-answer."}
                return

    yield {"type": "error", "message": str(last_error) if last_error else "No model available."}
