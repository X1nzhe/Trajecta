"""Pydantic contracts for Trajecta v1.

Keep this file aligned with ``docs/contracts.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


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
    # 1-based, matches the source dataset's step key and the screenshot
    # filename suffix (step.index=7 ↔ source key "7" ↔ "screenshot_007.png").
    # UI displays step.index directly without any offset conversion.
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
    case_id: str = Field(pattern=r"^fm_[a-z][a-z0-9_]*_[0-9]{3}$")
    failure_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    summary: str
    fix_hint: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_run_id: str | None = None


class FollowupSuggestion(BaseModel):
    """Agent-authored followup-question chip shown in the UI after analyze.

    Transport-only: these are passed as a kwarg of the terminal
    ``propose_eval_case`` tool call so the trace carries them through,
    but they are NOT persisted as part of the ``EvalCase`` schema. The
    frontend reads the latest ``propose_eval_case`` tool_call event's
    args. Bounded length to keep chip rendering predictable; max 4
    suggestions per call is enforced in ``tools.propose_eval_case``.
    """

    label: str = Field(min_length=1, max_length=40)
    message: str = Field(min_length=1, max_length=200)


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
    """A human-validated outcome record produced by the Eval Agent.

    Two valid shapes:

    - **Failure case** — all five failure fields populated (failure_step,
      failure_type, expected_behavior, actual_behavior, regression_rule).
    - **Success case** — all five failure fields are None. The case still
      carries task + evidence; the absence of failure fields is the
      semantic signal.

    Half-populated cases are rejected by the model_validator; mixing the
    two shapes would let callers ship "I know step 3 failed but I can't
    tell you how" records and there is no downstream consumer for that.
    """

    case_id: str
    source_run_id: str
    task: str
    failure_step: int | None = None
    failure_type: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    expected_behavior: str | None = None
    actual_behavior: str | None = None
    evidence: list[EvidenceItem]
    regression_rule: str | None = None
    retrieved_context_ids: list[str] = Field(default_factory=list)
    human_validated: bool = False

    @model_validator(mode="after")
    def _validate_failure_fields_consistency(self) -> "EvalCase":
        failure_fields = (
            self.failure_step,
            self.failure_type,
            self.expected_behavior,
            self.actual_behavior,
            self.regression_rule,
        )
        present = sum(1 for value in failure_fields if value is not None)
        if present not in (0, 5):
            raise ValueError(
                "EvalCase failure fields must be all present (failure case) "
                "or all absent (success case); "
                f"got {present}/5 populated"
            )
        return self

    @property
    def is_success(self) -> bool:
        return self.failure_type is None


class AgentTraceEvent(BaseModel):
    seq: int
    type: Literal["agent_message", "user_message", "tool_call", "tool_result", "tool_error", "phase"]
    name: str | None = None
    args: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    message: str | None = None
    error: str | None = None
    turn: int = 0


class TurnMetrics(BaseModel):
    """Per-turn breakdown of the cumulative AgentTrace counters.

    turn 0 == initial analyze; turn >= 1 == followups. The UI reads
    these to show "this turn cost X seconds / Y tokens" instead of the
    whole-session totals, which kept growing with each followup. The
    cumulative ``AgentTrace.runtime_ms`` etc. are still maintained for
    the SPEC.md cost-ablation demo and any downstream analytics.
    """

    turn: int
    runtime_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


class AgentTrace(BaseModel):
    run_id: str
    user_intent: Literal["analyze_run", "analyze_step"]
    selected_step: int | None = None
    tool_call_count: int = 0
    turn_count: int = 1
    terminated_by: Literal["propose_eval_case", "budget_exceeded", "error"] = "error"
    events: list[AgentTraceEvent] = Field(default_factory=list)
    # Per-trace observability — spec (docs/eval_agent.md "Observability")
    # requires the trace itself to be the observability surface, so cost
    # and latency counters live here, not on a separate APM. runtime_ms
    # is wall-clock for the agent loop (analyze + followups combined,
    # accumulated across turns). input_tokens / output_tokens come from
    # AIMessage.usage_metadata when the underlying client provides it
    # (real OpenAI path); offline mocks leave the fields at 0. VLM calls
    # (preprocess + get_step_detail) are NOT counted yet — they live
    # outside the agent's _invoke_model and need a separate hook.
    runtime_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Per-turn breakdown of the same counters above. Empty on traces
    # written before this field existed; new analyze/followup runs
    # append one entry per turn. The UI reads the latest turn for the
    # footer ("this turn") and turn 0 for the collapsed-trace summary
    # ("initial analyze") so neither display keeps growing with every
    # followup.
    turn_metrics: list[TurnMetrics] = Field(default_factory=list)
