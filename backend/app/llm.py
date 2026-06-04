"""VLM client factory shared by Trajectory Preprocessing and ``get_step_detail``.

The factory returns one of two duck-typed clients exposing:

- ``model_name: str``
- ``summarize_low_detail(image_bytes: bytes, *, image_name: str, action_type: str, step_index: int) -> str | None``
- ``summarize_high_detail(image_bytes: bytes, *, image_name: str, action_type: str, step_index: int, ...) -> str | None``

Screenshots live in SQLite as BLOBs (see ``backend.app.storage.load_screenshot``);
callers pass the raw bytes plus a stable identifier so the mock client can keep
its deterministic seeding. The real client is selected when ``OPENAI_API_KEY``
is set for the provider selected by ``TRAJECTA_VLM_MODEL``. ``gemini-*``
models use ``GEMINI_API_KEY`` and Gemini's OpenAI-compatible endpoint; all
other models use ``OPENAI_API_KEY`` and the default OpenAI-compatible endpoint.
Otherwise the deterministic mock is returned. This is the only client
construction path; ``preprocess.py`` and the ``get_step_detail`` tool must go
through ``get_vlm_client``.
"""

from __future__ import annotations

import base64
import contextvars
import hashlib
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Literal, Protocol

from backend.app import prompts as prompt_registry


logger = logging.getLogger(__name__)


GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


@dataclass(frozen=True)
class ModelProviderConfig:
    provider: Literal["gemini", "openai"]
    api_key: str | None
    base_url: str | None


def _env_value(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def resolve_model_provider(model_name: str | None) -> ModelProviderConfig:
    """Resolve API credentials for an OpenAI-compatible model id.

    ``gemini-*`` models route through Gemini's OpenAI-compatible endpoint.
    Everything else keeps the existing OpenAI-compatible behavior.
    """

    normalized = (model_name or "").strip().lower()
    if normalized.startswith("gemini-"):
        return ModelProviderConfig(
            provider="gemini",
            api_key=_env_value("GEMINI_API_KEY"),
            base_url=_env_value("GEMINI_BASE_URL") or GEMINI_OPENAI_BASE_URL,
        )
    return ModelProviderConfig(
        provider="openai",
        api_key=_env_value("OPENAI_API_KEY"),
        base_url=_env_value("OPENAI_BASE_URL"),
    )


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

_MAX_SUMMARY_CHARS = 300
_MAX_HIGH_DETAIL_CHARS = 1500

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
        task: str | None = None,
        action_label: str | None = None,
        action_text: str | None = None,
        action_raw: str | None = None,
        url: str | None = None,
        title: str | None = None,
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


def _normalize_structured_text(text: str | None, *, max_chars: int = _MAX_HIGH_DETAIL_CHARS) -> str | None:
    if text is None:
        return None
    lines = [" ".join(line.strip().split()) for line in text.replace("\r", "\n").split("\n")]
    collapsed = "\n".join(line for line in lines if line)
    if not collapsed:
        return None
    if len(collapsed) > max_chars:
        collapsed = collapsed[:max_chars]
    return collapsed


def _clip_context(value: str | None, *, max_chars: int = 500) -> str:
    if value is None:
        return "unavailable"
    collapsed = " ".join(str(value).replace("\r", " ").replace("\n", " ").split())
    if not collapsed:
        return "unavailable"
    if len(collapsed) > max_chars:
        return collapsed[:max_chars]
    return collapsed


def _build_high_detail_prompt(
    *,
    image_name: str,
    action_type: str,
    step_index: int,
    task: str | None = None,
    action_label: str | None = None,
    action_text: str | None = None,
    action_raw: str | None = None,
    url: str | None = None,
    title: str | None = None,
) -> str:
    bundle = prompt_registry.active_vlm_high_detail_prompt()
    variables = {
        "task": _clip_context(task, max_chars=800),
        "step_index": step_index,
        "image_name": _clip_context(image_name, max_chars=120),
        "action_type": _clip_context(action_type, max_chars=80),
        "action_label": _clip_context(action_label, max_chars=240),
        "action_text": _clip_context(action_text, max_chars=240),
        "action_raw": _clip_context(action_raw, max_chars=300),
        "url": _clip_context(url, max_chars=500),
        "title": _clip_context(title, max_chars=240),
    }
    try:
        return bundle.text.format(**variables)
    except KeyError as exc:
        missing = str(exc).strip("'")
        raise ValueError(
            f"VLM high-detail prompt version {bundle.version!r} references "
            f"unknown template variable {missing!r}"
        ) from exc


def active_high_detail_prompt_identity() -> tuple[str, str]:
    bundle = prompt_registry.active_vlm_high_detail_prompt()
    return bundle.version, bundle.sha256


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
        task: str | None = None,
        action_label: str | None = None,
        action_text: str | None = None,
        action_raw: str | None = None,
        url: str | None = None,
        title: str | None = None,
    ) -> str | None:
        del image_bytes, action_text, action_raw
        seed = f"{image_name}|{action_type}|{step_index}".encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        page = _PAGE_TYPES[digest[0] % len(_PAGE_TYPES)]
        layout = _LAYOUT_STRUCTURES[digest[1] % len(_LAYOUT_STRUCTURES)]
        overlay = _OVERLAY_STATES[digest[2] % len(_OVERLAY_STATES)]
        text_hint = _TEXT_HINTS[digest[3] % len(_TEXT_HINTS)]
        target_hint = _TARGET_HINTS[digest[4] % len(_TARGET_HINTS)]
        coord_status = _COORD_STATUSES[digest[5] % len(_COORD_STATUSES)]
        summary = (
            f"page_state: page={page}; layout={layout}; overlay={overlay}; "
            f"url={_clip_context(url, max_chars=80)}; title={_clip_context(title, max_chars=80)}\n"
            f"task_relevant_visible_text: mock text_hint={text_hint}\n"
            "selected_candidate: not_visible_in_mock\n"
            f"constraint_evidence: task={_clip_context(task, max_chars=120)}; status=not_visible_in_mock\n"
            f"action_target: action={action_type}; label={_clip_context(action_label, max_chars=80)}; "
            f"target_hint={target_hint}; coord_status={coord_status}; step={step_index}\n"
            "success_signals: unavailable_in_mock\n"
            "failure_signals: unavailable_in_mock\n"
            "uncertainty: mock VLM output is deterministic and not visual evidence"
        )
        return _normalize_structured_text(summary, max_chars=_MAX_HIGH_DETAIL_CHARS)


class RealVLMClient:
    """OpenAI-compatible VLM client.

    Only constructed when ``TRAJECTA_VLM_MODEL`` and the matching provider
    API key are both set. Network failures degrade to ``None`` so a single
    flaky call cannot abort an entire preprocessing run, but the underlying
    exception is logged at WARNING so silent VLM dead-paths don't go unnoticed
    (every "vlm_summary: null" in a trace should now have a corresponding log
    line explaining why).
    """

    def __init__(self, *, api_key: str, model_name: str, base_url: str | None = None) -> None:
        self._api_key = api_key
        self._base_url = base_url
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

        client_kwargs = {"api_key": self._api_key}
        if self._base_url is not None:
            client_kwargs["base_url"] = self._base_url
        client = OpenAI(**client_kwargs)
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
        task: str | None = None,
        action_label: str | None = None,
        action_text: str | None = None,
        action_raw: str | None = None,
        url: str | None = None,
        title: str | None = None,
    ) -> str | None:
        if not image_bytes:
            return None
        try:
            from openai import OpenAI
        except ImportError:
            return None

        client_kwargs = {"api_key": self._api_key}
        if self._base_url is not None:
            client_kwargs["base_url"] = self._base_url
        client = OpenAI(**client_kwargs)
        data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        prompt = _build_high_detail_prompt(
            image_name=image_name,
            action_type=action_type,
            step_index=step_index,
            task=task,
            action_label=action_label,
            action_text=action_text,
            action_raw=action_raw,
            url=url,
            title=title,
        )
        try:
            response = self._create_chat(
                client,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": data_url, "detail": "high"},
                            },
                        ],
                    }
                ],
                max_output_tokens=900,
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
        return _normalize_structured_text(text, max_chars=_MAX_HIGH_DETAIL_CHARS)


def get_vlm_client() -> VLMClient:
    """Return the active VLM client per environment configuration.

    Falls back to ``MockVLMClient`` whenever the real client cannot be
    constructed — missing env vars, or the ``openai`` package not being
    importable. Without this probe, a missing dependency would still
    produce a ``RealVLMClient`` whose calls all return ``None`` and whose
    ``preprocess_model`` would be cached into ``digest.json`` as if a real
    model had run, which is silently wrong.
    """

    model_name = os.environ.get("TRAJECTA_VLM_MODEL")
    provider = resolve_model_provider(model_name)
    if not (provider.api_key and model_name):
        return MockVLMClient()
    try:
        from openai import OpenAI  # noqa: F401  probe availability
    except ImportError:
        return MockVLMClient()
    return RealVLMClient(
        api_key=provider.api_key,
        model_name=model_name,
        base_url=provider.base_url,
    )
