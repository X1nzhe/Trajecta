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
import hashlib
import os
from typing import Protocol


# Phase 3c will reuse this verbatim for ``get_step_detail`` low-detail mode.
# Keep the wording stable and structural — no requests for text, labels, or
# small UI elements.
LOW_DETAIL_PROMPT = (
    "You are inspecting a single browser screenshot at very low resolution. "
    "Return a single-line, at-most 200-character structural hint covering: "
    "page type (one of: search_results, form, detail, dashboard, modal, "
    "loading, error, unknown), whether a modal or large overlay is present, "
    "whether a visually obvious error banner is present, and approximate "
    "focus region (top, center, bottom, left, right, unknown). "
    "Do NOT quote text, name buttons, or describe small UI elements; the "
    "resolution does not support it. Output exactly one line, no prose. "
    "Per docs/preprocessing.md, layout-vs-previous-step is intentionally "
    "out of scope here — each call is independent."
)

HIGH_DETAIL_PROMPT = (
    "You are inspecting one browser screenshot at high detail for an Eval Agent. "
    "Return one concise block, at most 500 characters, covering: page type and "
    "layout structure; visible text relevant to the action; target element "
    "identity; modal, overlay, or error state; and approximate coordinate "
    "region of the action target. Do not invent unseen evidence."
)

_MAX_SUMMARY_CHARS = 200
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
    cannot abort an entire preprocessing run.
    """

    def __init__(self, *, api_key: str, model_name: str) -> None:
        self._api_key = api_key
        self.model_name = model_name

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
            response = client.chat.completions.create(
                model=self.model_name,
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
                max_tokens=120,
                temperature=0,
            )
        except Exception:
            return None

        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError):
            return None
        if not isinstance(text, str):
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
            response = client.chat.completions.create(
                model=self.model_name,
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
                max_tokens=500,
                temperature=0,
            )
        except Exception:
            return None

        try:
            text = response.choices[0].message.content
        except (AttributeError, IndexError):
            return None
        if not isinstance(text, str):
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
