"""Versioned prompt registry for Trajecta.

Prompts are repo artifacts under ``prompts/<prompt_family>/<version>/``.
Runtime selection is intentionally simple: set the relevant environment
variable to a committed version directory, or omit it to use the default.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


PROMPT_ENV_VAR = "TRAJECTA_PROMPT_VERSION"
DEFAULT_PROMPT_VERSION = "v1_minimal"
VLM_HIGH_DETAIL_PROMPT_ENV_VAR = "TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION"
DEFAULT_VLM_HIGH_DETAIL_PROMPT_VERSION = "v1_task_context"

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_ROOT = REPO_ROOT / "prompts" / "eval_agent"
VLM_HIGH_DETAIL_PROMPT_ROOT = REPO_ROOT / "prompts" / "vlm_high_detail"
_PROMPT_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")


@dataclass(frozen=True)
class PromptBundle:
    version: str
    system: str
    followup: str
    sha256: str
    system_sha256: str
    followup_sha256: str


@dataclass(frozen=True)
class TextPromptBundle:
    version: str
    text: str
    sha256: str


def active_prompt_version() -> str:
    configured = os.environ.get(PROMPT_ENV_VAR, "").strip()
    return configured or DEFAULT_PROMPT_VERSION


def active_prompt_bundle() -> PromptBundle:
    return load_prompt_bundle(active_prompt_version())


def available_prompt_versions() -> list[str]:
    return _available_versions(PROMPT_ROOT)


def active_vlm_high_detail_prompt_version() -> str:
    configured = os.environ.get(VLM_HIGH_DETAIL_PROMPT_ENV_VAR, "").strip()
    return configured or DEFAULT_VLM_HIGH_DETAIL_PROMPT_VERSION


def active_vlm_high_detail_prompt() -> TextPromptBundle:
    return load_vlm_high_detail_prompt(active_vlm_high_detail_prompt_version())


def available_vlm_high_detail_prompt_versions() -> list[str]:
    return _available_versions(VLM_HIGH_DETAIL_PROMPT_ROOT)


@lru_cache(maxsize=16)
def load_prompt_bundle(version: str | None = None) -> PromptBundle:
    selected = (version or active_prompt_version()).strip()
    _validate_prompt_version(selected)
    prompt_dir = PROMPT_ROOT / selected
    system_path = prompt_dir / "system.md"
    followup_path = prompt_dir / "followup.md"
    try:
        system = system_path.read_text(encoding="utf-8").strip()
        followup = followup_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        available = ", ".join(available_prompt_versions()) or "<none>"
        raise FileNotFoundError(
            f"unknown prompt version {selected!r}; expected files under "
            f"{prompt_dir}. Available versions: {available}"
        ) from exc
    if not system:
        raise ValueError(f"prompt version {selected!r} has an empty system.md")
    if not followup:
        raise ValueError(f"prompt version {selected!r} has an empty followup.md")

    system_sha = _sha256(system)
    followup_sha = _sha256(followup)
    combined_sha = _sha256(
        "\n".join(
            [
                f"version={selected}",
                f"system_sha256={system_sha}",
                f"followup_sha256={followup_sha}",
            ]
        )
    )
    return PromptBundle(
        version=selected,
        system=system,
        followup=followup,
        sha256=combined_sha,
        system_sha256=system_sha,
        followup_sha256=followup_sha,
    )


@lru_cache(maxsize=16)
def load_vlm_high_detail_prompt(version: str | None = None) -> TextPromptBundle:
    selected = (version or active_vlm_high_detail_prompt_version()).strip()
    _validate_prompt_version(selected)
    prompt_dir = VLM_HIGH_DETAIL_PROMPT_ROOT / selected
    prompt_path = prompt_dir / "prompt.md"
    try:
        text = prompt_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        available = ", ".join(available_vlm_high_detail_prompt_versions()) or "<none>"
        raise FileNotFoundError(
            f"unknown VLM high-detail prompt version {selected!r}; expected "
            f"{prompt_path}. Available versions: {available}"
        ) from exc
    if not text:
        raise ValueError(f"VLM high-detail prompt version {selected!r} has an empty prompt.md")
    return TextPromptBundle(version=selected, text=text, sha256=_sha256(text))


def _validate_prompt_version(version: str) -> None:
    if not _PROMPT_VERSION_RE.fullmatch(version):
        raise ValueError(
            f"invalid prompt version {version!r}; use letters, numbers, dots, "
            "underscores, or hyphens only"
        )


def _available_versions(root: Path) -> list[str]:
    if not root.exists():
        return []
    return sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and _PROMPT_VERSION_RE.fullmatch(path.name)
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
