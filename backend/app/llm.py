"""VLM client factory shared by Trajectory Preprocessing and ``get_step_detail``.

The factory returns one of two duck-typed clients exposing:

- ``model_name: str``
- ``summarize_low_detail(image_path: Path, *, action_type: str, step_index: int) -> str | None``

The real client is selected when ``OPENAI_API_KEY`` is set AND
``TRAJECTA_VLM_MODEL`` is configured. Otherwise the deterministic mock is
returned. This is the only client construction path; ``preprocess.py`` and
the future ``get_step_detail`` tool must go through ``get_vlm_client``.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
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
    "resolution does not support it. Output exactly one line, no prose."
)

_MAX_SUMMARY_CHARS = 200

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


class VLMClient(Protocol):
    model_name: str

    def summarize_low_detail(
        self,
        image_path: Path,
        *,
        action_type: str,
        step_index: int,
    ) -> str | None: ...


def _normalize_summary(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if not collapsed:
        return None
    if len(collapsed) > _MAX_SUMMARY_CHARS:
        collapsed = collapsed[:_MAX_SUMMARY_CHARS]
    return collapsed


class MockVLMClient:
    """Deterministic, network-free VLM stub used by tests and offline runs.

    Output is fully determined by ``(image_path.name, action_type, step_index)``
    so the digest is byte-stable across rebuilds.
    """

    model_name = "mock"

    def summarize_low_detail(
        self,
        image_path: Path,
        *,
        action_type: str,
        step_index: int,
    ) -> str | None:
        seed = f"{image_path.name}|{action_type}|{step_index}".encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        bucket = int.from_bytes(digest[:4], "big")
        page = _PAGE_TYPES[bucket % len(_PAGE_TYPES)]
        focus = _FOCUS_REGIONS[bucket % len(_FOCUS_REGIONS)]
        summary = (
            f"page={page} overlay=none error=none focus={focus} "
            f"action={action_type} step={step_index}"
        )
        return _normalize_summary(summary)


class RealVLMClient:
    """OpenAI-compatible low-detail VLM client.

    Only constructed when ``OPENAI_API_KEY`` and ``TRAJECTA_VLM_MODEL`` are
    both set. Network failures degrade to ``None`` so a single flaky call
    cannot abort an entire preprocessing run.
    """

    def __init__(self, *, api_key: str, model_name: str) -> None:
        self._api_key = api_key
        self.model_name = model_name

    def summarize_low_detail(
        self,
        image_path: Path,
        *,
        action_type: str,
        step_index: int,
    ) -> str | None:
        try:
            image_bytes = image_path.read_bytes()
        except OSError:
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


def get_vlm_client() -> VLMClient:
    """Return the active VLM client per environment configuration."""

    api_key = os.environ.get("OPENAI_API_KEY")
    model_name = os.environ.get("TRAJECTA_VLM_MODEL")
    if api_key and model_name:
        return RealVLMClient(api_key=api_key, model_name=model_name)
    return MockVLMClient()
