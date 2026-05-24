"""Trajectory Preprocessing — deterministic stage that produces a TrajectoryDigest.

This stage runs **before** the Eval Agent. It does no retrieval, no failure
labeling, and no LangGraph. See ``docs/preprocessing.md`` for the contract.
"""

from __future__ import annotations

from pathlib import Path

from backend.app import storage
from backend.app.coordinate_validator import validate_coordinates
from backend.app.llm import VLMClient, get_vlm_client
from backend.app.schemas import (
    StepAction,
    StepDigest,
    StepObservation,
    TrajectoryDigest,
    TrajectoryRun,
    TrajectoryStep,
)


PREPROCESS_VERSION = "v1"


def build_digest(run: TrajectoryRun, *, client: VLMClient | None = None) -> TrajectoryDigest:
    """Build a fresh ``TrajectoryDigest`` from a validated ``TrajectoryRun``."""

    validated = TrajectoryRun.model_validate(run)
    if not validated.steps:
        raise ValueError(f"cannot preprocess run {validated.run_id!r}: no steps")

    vlm = client if client is not None else get_vlm_client()

    step_digests: list[StepDigest] = [
        _build_step_digest(validated.run_id, step, vlm) for step in validated.steps
    ]

    return TrajectoryDigest(
        run_id=validated.run_id,
        task=validated.task,
        step_count=len(step_digests),
        steps=step_digests,
        preprocess_model=vlm.model_name,
        preprocess_version=PREPROCESS_VERSION,
    )


def load_or_build_digest(run_id: str) -> TrajectoryDigest:
    """Return the cached digest if it matches the active client; rebuild otherwise."""

    run = storage.load_run(run_id)
    client = get_vlm_client()

    cached = storage.load_digest(run_id)
    if (
        cached is not None
        and cached.preprocess_version == PREPROCESS_VERSION
        and cached.preprocess_model == client.model_name
    ):
        return cached

    digest = build_digest(run, client=client)
    storage.save_digest(run_id, digest)
    return digest


def _build_step_digest(run_id: str, step: TrajectoryStep, client: VLMClient) -> StepDigest:
    observation = step.observation
    screenshot_path = _resolve_screenshot(run_id, observation.screenshot)
    has_screenshot = screenshot_path is not None

    raw_width = _coerce_int(step.metadata.get("image_width"))
    raw_height = _coerce_int(step.metadata.get("image_height"))
    coord_status = validate_coordinates(
        step.action,
        image_path=screenshot_path,
        image_width=raw_width,
        image_height=raw_height,
    ).status

    if _has_source_text(observation) or not has_screenshot:
        summary: str | None = None
    else:
        summary = client.summarize_low_detail(
            screenshot_path,
            action_type=step.action.type,
            step_index=step.index,
        )

    return StepDigest(
        index=step.index,
        action_type=step.action.type,
        action_text=_render_action_text(step.action),
        action_target=_action_target(step.action),
        url=observation.url,
        title=observation.title,
        result_status=step.result.status,
        coord_validation_status=coord_status,
        vlm_low_detail_summary=summary,
        has_screenshot=has_screenshot,
    )


def _resolve_screenshot(run_id: str, filename: str | None) -> Path | None:
    if not filename:
        return None
    try:
        path = storage.screenshot_path(run_id, filename)
    except ValueError:
        return None
    return path if path.exists() and path.is_file() else None


def _has_source_text(observation: StepObservation) -> bool:
    return bool(observation.visible_text and observation.visible_text.strip())


def _render_action_text(action: StepAction) -> str:
    if action.type == "click":
        if action.coordinates is not None:
            return f"click at ({_fmt_num(action.coordinates.x)}, {_fmt_num(action.coordinates.y)})"
        return "click"
    if action.type == "type":
        if action.text:
            return f"type {action.text!r}"
        return "type"
    if action.type == "scroll":
        if action.coordinates is not None:
            return f"scroll at ({_fmt_num(action.coordinates.x)}, {_fmt_num(action.coordinates.y)})"
        return "scroll"
    if action.type == "navigate":
        if action.text:
            return f"navigate to {action.text!r}"
        return "navigate"
    if action.type == "wait":
        return "wait"
    fallback = action.label or action.raw or action.type
    return fallback


def _action_target(action: StepAction) -> str | None:
    if action.label and action.label.strip():
        return action.label.strip()
    return None


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _fmt_num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"
