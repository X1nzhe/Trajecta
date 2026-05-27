"""VLM client factory shared by Trajectory Preprocessing and ``get_step_detail``.

The factory returns one of two duck-typed clients exposing:

- ``model_name: str``
- ``summarize_low_detail(image_bytes: bytes, *, image_name: str, action_type: str, step_index: int) -> str | None``
- ``summarize_high_detail(image_bytes: bytes, *, image_name: str, action_type: str, step_index: int) -> str | None``

Screenshots live in SQLite as BLOBs (see ``backend.app.storage.load_screenshot``);
callers pass the raw bytes plus a stable identifier so the mock client can keep
its deterministic seeding. The real client is selected when ``OPENAI_API_KEY``
is set AND ``TRAJECTA_VLM_MODEL`` is configured. Otherwise the deterministic
mock is returned. This is the only client construction path; ``preprocess.py``
and the ``get_step_detail`` tool must go through ``get_vlm_client``.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import logging
import os
from contextlib import contextmanager
from typing import Iterator, Protocol


logger = logging.getLogger(__name__)


# --- VLM usage accounting ---------------------------------------------------
#
# preprocess.build_digest and the get_step_detail tool both run inside outer
# scopes (stream_analyze, stream_followup, load_or_build_digest). Those outer
# scopes care about TOTAL VLM tokens spent — but adding usage to every VLM
# call's return value would bleed through 5+ files and pollute the prompts the
# agent sees. Instead, the outer scope opens a `vlm_usage_scope()` and the
# VLM client increments the active bucket on every successful call.
# When the scope closes, the outer code reads totals and stamps them onto
# TrajectoryDigest / AgentTrace. Nested scopes are supported via a stack-like
# reset; if no scope is active, record_vlm_usage() is a no-op (so unit tests
# that call the client directly don't crash).
_VLM_USAGE_BUCKET: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "trajecta_vlm_usage_bucket",
    default=None,
)


@contextmanager
def vlm_usage_scope() -> Iterator[dict[str, int]]:
    """Push a fresh {input, output} accumulator. Yields the dict so the
    caller can read totals after the block. Restores the previous scope on
    exit via ``set(previous)`` (NOT ``reset(token)`` — tokens are tied to
    the context they were created in, and FastAPI's StreamingResponse
    consumes our sync generators across async iteration boundaries, which
    breaks ``reset`` with `<Token> was created in a different Context`).
    Save/restore is robust to that and still nestable."""
    bucket: dict[str, int] = {"input": 0, "output": 0}
    previous = _VLM_USAGE_BUCKET.get()
    _VLM_USAGE_BUCKET.set(bucket)
    try:
        yield bucket
    finally:
        _VLM_USAGE_BUCKET.set(previous)


def record_vlm_usage(input_tokens: int, output_tokens: int) -> None:
    """Increment the active accumulator. No-op when no scope is active."""
    bucket = _VLM_USAGE_BUCKET.get()
    if bucket is None:
        return
    bucket["input"] += max(0, input_tokens)
    bucket["output"] += max(0, output_tokens)


def _extract_openai_usage(response: object) -> tuple[int, int]:
    """Pull (prompt_tokens, completion_tokens) off an OpenAI chat completion
    response. Returns (0, 0) when the SDK shape changes or the field is
    missing — the VLM call still succeeds; we just lose accounting for that
    one call. Logged at DEBUG (not WARNING) so it doesn't drown the trace
    log for transient SDK quirks."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        return prompt, completion
    except Exception as exc:  # pragma: no cover - defensive only
        logger.debug("VLM usage extraction failed: %s: %s", type(exc).__name__, exc)
        return 0, 0


# Phase 3c will reuse this verbatim for ``get_step_detail`` low-detail mode.
# Keep the wording stable and structural — no requests for text, labels, or
# small UI elements.
LOW_DETAIL_PROMPT = (
    "You are inspecting one browser screenshot at low resolution for an "
    "Eval Agent. Return ONE line, at most 300 characters, with TWO "
    "segments separated by ' | ':\n"
    "  TAGS: page_type=<one of: search_results, form, detail, dashboard, "
    "modal, loading, error, unknown>; modal=<yes|no>; "
    "error_banner=<yes|no>; focus=<top|center|bottom|left|right|unknown>\n"
    "  CUE: up to about 20 words naming the MOST PROMINENT legible "
    "content — hero headline, large image subject, big button label, "
    "empty-state copy, item count, or whatever visually distinguishes "
    "this page from a generic page of its type.\n"
    "Example: 'page_type=detail; modal=no; error_banner=no; focus=center "
    "| large product image, Add to Cart button visible, price prominently "
    "shown'\n"
    "Do NOT transcribe small UI labels, body paragraphs, table cells, or "
    "footer links — the resolution does not support reliable transcription. "
    "Do NOT compare to previous steps; each call is independent. Output "
    "ONE line, no prose, no markdown."
)

HIGH_DETAIL_PROMPT = (
    "You are inspecting one browser screenshot at high detail for an Eval Agent. "
    "Return one concise block, at most 500 characters, covering: page type and "
    "layout structure; visible text relevant to the action; target element "
    "identity; modal, overlay, or error state; and approximate coordinate "
    "region of the action target. Do not invent unseen evidence."
)

_MAX_SUMMARY_CHARS = 300
_MAX_HIGH_DETAIL_CHARS = 500

_PAGE_TYPES = (
    "unknown",
    "search_results",
    "form",
    "detail",
    "dashboard",
    "modal",
    "loading",
    "error",
)
_FOCUS_REGIONS = ("top", "center", "bottom", "left", "right", "unknown")
_LAYOUT_STRUCTURES = ("single_column", "two_column", "sidebar_main", "header_content", "dialog")
_OVERLAY_STATES = ("none", "modal", "toast", "blocking_overlay", "error_banner")
_TEXT_HINTS = ("none", "navigation", "form_labels", "result_titles", "error_copy")
_TARGET_HINTS = ("primary_button", "link", "input", "menu_item", "page_region")
_COORD_STATUSES = ("top_left", "top_right", "center", "bottom_left", "bottom_right", "unknown")


class VLMClient(Protocol):
    model_name: str

    def summarize_low_detail(
        self,
        image_bytes: bytes,
        *,
        image_name: str,
        action_type: str,
        step_index: int,
    ) -> str | None: ...

    def summarize_high_detail(
        self,
        image_bytes: bytes,
        *,
        image_name: str,
        action_type: str,
        step_index: int,
    ) -> str | None: ...


def _normalize_summary(text: str | None, *, max_chars: int = _MAX_SUMMARY_CHARS) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not collapsed:
        return None
    if len(collapsed) > max_chars:
        collapsed = collapsed[:max_chars]
    return collapsed


class MockVLMClient:
    """Deterministic, network-free VLM stub used by tests and offline runs.

    Output is fully determined by ``(image_name, action_type, step_index)``
    so the digest is byte-stable across rebuilds. ``image_bytes`` is accepted
    for API symmetry but intentionally unused — the test corpus relies on
    name-based determinism, not content-based hashing.
    """

    model_name = "mock"

    def summarize_low_detail(
        self,
        image_bytes: bytes,
        *,
        image_name: str,
        action_type: str,
        step_index: int,
    ) -> str | None:
        del image_bytes
        seed = f"{image_name}|{action_type}|{step_index}".encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        bucket = int.from_bytes(digest[:4], "big")
        page = _PAGE_TYPES[bucket % len(_PAGE_TYPES)]
        focus = _FOCUS_REGIONS[bucket % len(_FOCUS_REGIONS)]
        summary = (
            f"page={page} overlay=none error=none focus={focus} "
            f"action={action_type} step={step_index}"
        )
        return _normalize_summary(summary)

    def summarize_high_detail(
        self,
        image_bytes: bytes,
        *,
        image_name: str,
        action_type: str,
        step_index: int,
    ) -> str | None:
        del image_bytes
        seed = f"{image_name}|{action_type}|{step_index}".encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        page = _PAGE_TYPES[digest[0] % len(_PAGE_TYPES)]
        layout = _LAYOUT_STRUCTURES[digest[1] % len(_LAYOUT_STRUCTURES)]
        overlay = _OVERLAY_STATES[digest[2] % len(_OVERLAY_STATES)]
        text_hint = _TEXT_HINTS[digest[3] % len(_TEXT_HINTS)]
        target_hint = _TARGET_HINTS[digest[4] % len(_TARGET_HINTS)]
        coord_status = _COORD_STATUSES[digest[5] % len(_COORD_STATUSES)]
        summary = (
            f"page={page} layout={layout} overlay={overlay} text_hint={text_hint} "
            f"target_hint={target_hint} coord_status={coord_status} "
            f"action={action_type} step={step_index}"
        )
        return _normalize_summary(summary, max_chars=_MAX_HIGH_DETAIL_CHARS)


class RealVLMClient:
    """OpenAI-compatible VLM client.

    Only constructed when ``OPENAI_API_KEY`` and ``TRAJECTA_VLM_MODEL`` are
    both set. Network failures degrade to ``None`` so a single flaky call
    cannot abort an entire preprocessing run, but the underlying exception
    is logged at WARNING so silent VLM dead-paths don't go unnoticed
    (every "vlm_summary: null" in a trace should now have a corresponding
    log line explaining why).
    """

    def __init__(self, *, api_key: str, model_name: str) -> None:
        self._api_key = api_key
        self.model_name = model_name

    def _create_chat(self, client, *, messages: list, max_output_tokens: int):
        """Wrap chat.completions.create with adaptive max-tokens kwarg.

        OpenAI's newer reasoning-capable models (gpt-5.x, o1, o3, o4)
        renamed ``max_tokens`` → ``max_completion_tokens`` and reject the
        old name with HTTP 400 ``unsupported_parameter``. The kwarg name
        depends on the model and isn't always discoverable from the
        model id alone (preview / dated variants vary). Strategy:

        1. Prefer ``max_completion_tokens`` (the forward-compatible name).
        2. On the specific ``unsupported_parameter`` error, fall back to
           ``max_tokens`` and retry once.
        3. Any other error propagates and gets logged by the caller.
        """

        try:
            return client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                max_completion_tokens=max_output_tokens,
                temperature=0,
            )
        except Exception as exc:
            if "max_completion_tokens" in str(exc) and "unsupported" in str(exc).lower():
                # Older model — retry with the legacy kwarg.
                return client.chat.completions.create(
                    model=self.model_name,
                    messages=messages,
                    max_tokens=max_output_tokens,
                    temperature=0,
                )
            raise

    def summarize_low_detail(
        self,
        image_bytes: bytes,
        *,
        image_name: str,
        action_type: str,
        step_index: int,
    ) -> str | None:
        del image_name, action_type, step_index
        if not image_bytes:
            return None
        try:
            from openai import OpenAI
        except ImportError:
            return None

        client = OpenAI(api_key=self._api_key)
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        try:
            response = self._create_chat(
                client,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": LOW_DETAIL_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url, "detail": "low"},
                            },
                        ],
                    }
                ],
                max_output_tokens=120,
            )
        except Exception as exc:
            # Was silent: vlm_summary=null in the trace looked like a
            # mystery. Log with the model name + exception type so the
            # operator can immediately see whether it's a 4xx / network /
            # model-not-found / model-doesn't-support-vision issue.
            logger.warning(
                "VLM low-detail call failed (model=%s): %s: %s",
                self.model_name, type(exc).__name__, exc,
            )
            return None

        # Capture usage BEFORE attempting to extract content — the API call
        # already happened and was billed even if message.content is missing.
        prompt_tokens, completion_tokens = _extract_openai_usage(response)
        record_vlm_usage(prompt_tokens, completion_tokens)

        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError):
            logger.warning(
                "VLM low-detail response had no message.content (model=%s, response=%r)",
                self.model_name, response,
            )
            return None
        if not isinstance(text, str):
            logger.warning(
                "VLM low-detail returned non-string content (model=%s, type=%s)",
                self.model_name, type(text).__name__,
            )
            return None
        return _normalize_summary(text)

    def summarize_high_detail(
        self,
        image_bytes: bytes,
        *,
        image_name: str,
        action_type: str,
        step_index: int,
    ) -> str | None:
        del image_name, action_type, step_index
        if not image_bytes:
            return None
        try:
            from openai import OpenAI
        except ImportError:
            return None

        client = OpenAI(api_key=self._api_key)
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        try:
            response = self._create_chat(
                client,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": HIGH_DETAIL_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url, "detail": "high"},
                            },
                        ],
                    }
                ],
                max_output_tokens=500,
            )
        except Exception as exc:
            logger.warning(
                "VLM high-detail call failed (model=%s): %s: %s",
                self.model_name, type(exc).__name__, exc,
            )
            return None

        prompt_tokens, completion_tokens = _extract_openai_usage(response)
        record_vlm_usage(prompt_tokens, completion_tokens)

        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError):
            logger.warning(
                "VLM high-detail response had no message.content (model=%s, response=%r)",
                self.model_name, response,
            )
            return None
        if not isinstance(text, str):
            logger.warning(
                "VLM high-detail returned non-string content (model=%s, type=%s)",
                self.model_name, type(text).__name__,
            )
            return None
        return _normalize_summary(text, max_chars=_MAX_HIGH_DETAIL_CHARS)


def get_vlm_client() -> VLMClient:
    """Return the active VLM client per environment configuration.

    Falls back to ``MockVLMClient`` whenever the real client cannot be
    constructed — missing env vars, or the ``openai`` package not being
    importable. Without this probe, a missing dependency would still
    produce a ``RealVLMClient`` whose calls all return ``None`` and whose
    ``preprocess_model`` would be cached into ``digest.json`` as if a real
    model had run, which is silently wrong.
    """

    api_key = os.environ.get("OPENAI_API_KEY")
    model_name = os.environ.get("TRAJECTA_VLM_MODEL")
    if not (api_key and model_name):
        return MockVLMClient()
    try:
        from openai import OpenAI  # noqa: F401  probe availability
    except ImportError:
        return MockVLMClient()
    return RealVLMClient(api_key=api_key, model_name=model_name)
