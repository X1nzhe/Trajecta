"""Pydantic contracts for Trajecta v1.

Keep this file aligned with ``docs/contracts.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Coordinate(BaseModel):
    x: float
    y: float


class BBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class StepAction(BaseModel):
    type: Literal["click", "type", "scroll", "navigate", "wait", "unknown"]
    label: str | None = None
    text: str | None = None
    coordinates: Coordinate | None = None
    bbox: BBox | None = None
    raw: str | None = None


class StepObservation(BaseModel):
    screenshot: str | None = None
    url: str | None = None
    title: str | None = None
    visible_text: str | None = None
    visual_evidence: list[str] = Field(default_factory=list)


class StepResult(BaseModel):
    status: Literal["success", "failed", "unknown"] = "unknown"
    error: str | None = None


class CoordinateValidation(BaseModel):
    status: Literal["validated", "out_of_bounds", "missing", "unknown"] = "unknown"
    image_width: int | None = None
    image_height: int | None = None
    reason: str | None = None


class TrajectoryStep(BaseModel):
    index: int
    timestamp: str | None = None
    observation: StepObservation
    action: StepAction
    result: StepResult = Field(default_factory=StepResult)
    coordinate_validation: CoordinateValidation = Field(default_factory=CoordinateValidation)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TrajectoryRun(BaseModel):
    run_id: str
    task: str
    source: str = "allenai/MolmoWeb-HumanSkills"
    status: Literal["success", "failed", "unknown"] = "unknown"
    steps: list[TrajectoryStep]
    metadata: dict[str, Any] = Field(default_factory=dict)


class StepDigest(BaseModel):
    index: int
    action_type: Literal["click", "type", "scroll", "navigate", "wait", "unknown"]
    action_text: str
    action_target: str | None = None
    url: str | None = None
    title: str | None = None
    result_status: Literal["success", "failed", "unknown"] = "unknown"
    coord_validation_status: Literal["validated", "out_of_bounds", "missing", "unknown"] = "unknown"
    vlm_low_detail_summary: str | None = None
    has_screenshot: bool = False


class TrajectoryDigest(BaseModel):
    run_id: str
    task: str
    step_count: int
    steps: list[StepDigest]
    preprocess_model: str | None = None
    preprocess_version: str = "v1"


class FailureMemoryCase(BaseModel):
    case_id: str
    failure_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    summary: str
    fix_hint: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_run_id: str | None = None


class EvidenceItem(BaseModel):
    claim: str
    source: Literal[
        "trajectory",
        "trajectory_digest",
        "step_detail_high",
        "step_detail_low",
        "failure_memory",
        "eval_case",
        "successful_run",
        "unavailable",
    ]
    run_id: str | None = None
    step_index: int | None = None
    trace_event_seq: int | None = None
    context_id: str | None = None


class EvalCase(BaseModel):
    case_id: str
    source_run_id: str
    task: str
    failure_step: int
    failure_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expected_behavior: str
    actual_behavior: str
    evidence: list[EvidenceItem]
    regression_rule: str
    retrieved_context_ids: list[str] = Field(default_factory=list)
    human_validated: bool = False


class AgentTraceEvent(BaseModel):
    seq: int
    type: Literal["agent_message", "user_message", "tool_call", "tool_result", "tool_error"]
    name: str | None = None
    args: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    message: str | None = None
    error: str | None = None
    turn: int = 0


class AgentTrace(BaseModel):
    run_id: str
    user_intent: Literal["analyze_run", "analyze_step"]
    selected_step: int | None = None
    tool_call_count: int = 0
    turn_count: int = 1
    terminated_by: Literal["propose_eval_case", "budget_exceeded", "error"] = "error"
    events: list[AgentTraceEvent] = Field(default_factory=list)
