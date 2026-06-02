"""Trajectory Preprocessing — deterministic stage that produces a TrajectoryDigest.

This stage runs **before** the Eval Agent. It does no retrieval, no failure
labeling, and no LangGraph. See ``docs/preprocessing.md`` for the contract.
"""

from __future__ import annotations

from backend.app import storage
from backend.app.coordinate_validator import validate_coordinates
from backend.app.llm import VLMClient, get_vlm_client, vlm_usage_scope
from backend.app.schemas import (
    StepAction,
    StepDigest,
    StepObservation,
    TrajectoryDigest,
    Trajectory,
    TrajectoryStep,
)


PREPROCESS_VERSION = "v2"


def build_digest(trajectory: Trajectory, *, client: VLMClient | None = None) -> TrajectoryDigest:
    """Build a fresh ``TrajectoryDigest`` from a validated ``Trajectory``."""

    validated = Trajectory.model_validate(trajectory)
    if not validated.steps:
        raise ValueError(f"cannot preprocess trajectory {validated.trajectory_id!r}: no steps")

    vlm = client if client is not None else get_vlm_client()

    # Wrap the per-step VLM calls in a usage scope so the real client can
    # accumulate prompt/completion tokens into a single bucket. Mock client
    # is a no-op against the recorder, so the bucket stays at 0.
    with vlm_usage_scope() as vlm_usage:
        step_digests: list[StepDigest] = [
            _build_step_digest(validated.trajectory_id, step, vlm) for step in validated.steps
        ]

    return TrajectoryDigest(
        trajectory_id=validated.trajectory_id,
        task=validated.task,
        step_count=len(step_digests),
        steps=step_digests,
        preprocess_model=vlm.model_name,
        preprocess_version=PREPROCESS_VERSION,
        vlm_input_tokens=vlm_usage["input"],
        vlm_output_tokens=vlm_usage["output"],
    )


def load_or_build_digest(trajectory_id: str) -> TrajectoryDigest:
    """Return the cached digest if it matches the active client; rebuild otherwise."""

    trajectory = storage.load_trajectory(trajectory_id)
    client = get_vlm_client()

    cached = storage.load_digest(trajectory_id)
    if (
        cached is not None
        and cached.preprocess_version == PREPROCESS_VERSION
        and cached.preprocess_model == client.model_name
    ):
        return cached

    digest = build_digest(trajectory, client=client)
    storage.save_digest(trajectory_id, digest)
    return digest


def _build_step_digest(trajectory_id: str, step: TrajectoryStep, client: VLMClient) -> StepDigest:
    observation = step.observation
    screenshot_bytes = _load_screenshot_bytes(trajectory_id, observation.screenshot)
    has_screenshot = screenshot_bytes is not None

    raw_width = _coerce_int(step.metadata.get("image_width"))
    raw_height = _coerce_int(step.metadata.get("image_height"))
    coord_status = validate_coordinates(
        step.action,
        image_bytes=screenshot_bytes,
        image_width=raw_width,
        image_height=raw_height,
    ).status

    if _has_source_text(observation) or not has_screenshot:
        summary: str | None = None
    else:
        summary = client.summarize_low_detail(
            screenshot_bytes,
            image_name=observation.screenshot or f"step_{step.index}",
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


def _load_screenshot_bytes(trajectory_id: str, filename: str | None) -> bytes | None:
    if not filename:
        return None
    return storage.load_screenshot(trajectory_id, filename)


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
