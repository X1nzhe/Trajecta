"""Pydantic contracts for Trajecta v1.

Keep this file aligned with ``docs/contracts.md``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

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
    # Cumulative VLM token usage spent producing this digest's low-detail
    # summaries. Captured via vlm_usage_scope() in preprocess.build_digest.
    # Mock path leaves them at 0. Old digests deserialize with defaults.
    vlm_input_tokens: int = 0
    vlm_output_tokens: int = 0


# v1 failure-type vocabulary — the closed set agents must choose from
# when filling ``EvalCase.failure_type``. Anything outside this set is
# rejected by ``tools.propose_eval_case`` so the agent can't invent
# labels like ``wrong_destination`` or ``unsupported_answer`` (seen in
# eval runs). Mirrored by ``agent_eval.V1_FAILURE_VOCABULARY`` (kept in
# that module for the baseline math so it stays self-contained).
#
# The ``V1FailureType`` Literal alias is the type hint used in
# ``tools.propose_eval_case``. LangChain ``bind_tools`` walks the
# function signature, builds a Pydantic model, and emits a JSON Schema
# whose ``failure_type`` property includes ``enum: [...the 5 values]``.
# OpenAI strict-mode tool calls then constrain decoding to those tokens,
# so the agent cannot emit ``early_termination`` (typo) or
# ``wrong_destination`` (paraphrase) — the misspellings observed in past
# eval runs. The ``frozenset`` is the runtime defense-in-depth check.
V1FailureType = Literal[
    "early_terminated",
    "wrong_target",
    "wrong_result",
    "missed_constraint",
    "inefficient_search",
]
V1_FAILURE_VOCABULARY: frozenset[str] = frozenset({
    "early_terminated",
    "wrong_target",
    "wrong_result",
    "missed_constraint",
    "inefficient_search",
})


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
    # The LLM that produced this trace. Stamped once at initial
    # stream_analyze() so followups inherit the same value. Mirrors how
    # TrajectoryDigest exposes preprocess_model for the VLM side. Old
    # persisted traces deserialize with model=None (backwards compatible).
    model: str | None = None
    # Versioned prompt identity for reproducibility. ``prompt_version`` is the
    # committed directory under prompts/eval_agent/, and ``prompt_sha256`` is a
    # combined hash of the system + followup prompt files. Old traces default
    # to None; new traces stamp both at initial analyze.
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    # The VLM that backed `get_step_detail` calls within this trace, plus
    # cumulative token usage across all calls (initial + followups).
    # `vlm_model` is stamped at trace creation from TRAJECTA_VLM_MODEL —
    # we only attribute a VLM-id when the real client is reachable
    # (matches how `model` is stamped). Old traces default to None / 0.
    vlm_model: str | None = None
    vlm_input_tokens: int = 0
    vlm_output_tokens: int = 0
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


# ---------------------------------------------------------------------------
# Phase 8 A1 — Golden set
#
# The golden set is the S18 § 2.2 Build 1 deliverable. Rows live in
# eval/golden.jsonl and are produced from data/triage_notes.csv by
# scripts/build_golden_jsonl.py. The structured Fact union lets eval/judge.py
# evaluate 5 of 6 rubric clauses mechanically against the proposed EvalCase,
# leaving only clause 6 (evidence traceability) for the LLM judge. See
# docs/testing.md § Golden Set for the build rules and field semantics.

#: The five v1 failure types. Mirrors V1_FAILURE_VOCABULARY in
#: backend/app/agent_eval.py — kept in sync by hand. If the vocabulary ever
#: grows, both constants and FailureTypeFact's _validate_value need updating.
V1_FAILURE_VOCABULARY: tuple[str, ...] = (
    "early_terminated",
    "wrong_target",
    "wrong_result",
    "missed_constraint",
    "inefficient_search",
)


class OutcomeFact(BaseModel):
    """Predicate over EvalCase.is_success.

    Acceptable when ``proposed_is_success == (value == "success")``.
    Used in both expected_facts (must hold) and forbidden_facts (must not
    hold) rows.
    """

    field: Literal["outcome"]
    op: Literal["eq"]
    value: Literal["success", "failed"]


class FailureTypeFact(BaseModel):
    """Predicate over EvalCase.failure_type membership in a set.

    ``value`` is a non-empty subset of V1_FAILURE_VOCABULARY. For failed
    samples the expected_facts entry carries the labelled multi-label set
    (multi-label OR is acceptable per S18 grading); the forbidden_facts
    entry carries V1_FAILURE_VOCABULARY \\ labelled_set so a stray
    misclassification fails clause 5.
    """

    field: Literal["failure_type"]
    op: Literal["in"]
    value: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_value(self) -> "FailureTypeFact":
        invalid = [v for v in self.value if v not in V1_FAILURE_VOCABULARY]
        if invalid:
            raise ValueError(
                f"FailureTypeFact.value contains unknown failure types: {invalid!r}; "
                f"allowed: {V1_FAILURE_VOCABULARY!r}"
            )
        return self


class FailureStepFact(BaseModel):
    """Predicate over EvalCase.failure_step locality.

    Inclusive interval. Build rule (docs/testing.md § Golden Set) widens the
    labelled step by ±2; the judge accepts a proposed step inside the
    interval as clause 3 satisfied.
    """

    field: Literal["failure_step"]
    op: Literal["in_range"]
    value: tuple[int, int]

    @model_validator(mode="after")
    def _validate_value(self) -> "FailureStepFact":
        lo, hi = self.value
        if lo > hi:
            raise ValueError(
                f"FailureStepFact.value must satisfy min <= max; got ({lo}, {hi})"
            )
        if lo < 0:
            raise ValueError(
                f"FailureStepFact.value bounds must be non-negative; got ({lo}, {hi})"
            )
        return self


Fact = Annotated[
    Union[OutcomeFact, FailureTypeFact, FailureStepFact],
    Field(discriminator="field"),
]


class GoldenInput(BaseModel):
    run_id: str = Field(min_length=1)
    intent: Literal["analyze_run", "analyze_step"] = "analyze_run"


class GoldenCase(BaseModel):
    """One row of eval/golden.jsonl.

    Built from data/triage_notes.csv by scripts/build_golden_jsonl.py.
    Validation rules (model_validator) catch shapes the CSV-to-JSONL
    builder must never emit:

      - expected_facts and forbidden_facts must be disjoint (an entry that
        contradicts itself collapses the judge's mechanical decision)
      - failure-shape rows carry both an OutcomeFact("failed") and a
        FailureTypeFact in expected_facts (otherwise clause 2 has nothing
        to match against)
      - success-shape rows carry only an OutcomeFact("success") in
        expected_facts (no FailureTypeFact / FailureStepFact)
      - tags is non-empty
    """

    input: GoldenInput
    expected_facts: list[Fact] = Field(min_length=1)
    forbidden_facts: list[Fact] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_shape(self) -> "GoldenCase":
        # Disjointness: a fact that appears in both lists is a builder bug.
        expected_repr = {self._fact_key(f) for f in self.expected_facts}
        forbidden_repr = {self._fact_key(f) for f in self.forbidden_facts}
        overlap = expected_repr & forbidden_repr
        if overlap:
            raise ValueError(
                f"expected_facts and forbidden_facts must be disjoint; "
                f"overlap: {sorted(overlap)}"
            )

        # Success vs failure shape consistency.
        outcome_facts = [f for f in self.expected_facts if isinstance(f, OutcomeFact)]
        if not outcome_facts:
            raise ValueError("expected_facts must include exactly one OutcomeFact")
        if len(outcome_facts) > 1:
            raise ValueError("expected_facts must include at most one OutcomeFact")
        outcome = outcome_facts[0].value

        has_ftype = any(isinstance(f, FailureTypeFact) for f in self.expected_facts)
        has_fstep = any(isinstance(f, FailureStepFact) for f in self.expected_facts)

        if outcome == "success":
            if has_ftype or has_fstep:
                raise ValueError(
                    "success-shape GoldenCase must not include FailureTypeFact "
                    "or FailureStepFact in expected_facts"
                )
        else:  # outcome == "failed"
            if not has_ftype:
                raise ValueError(
                    "failed-shape GoldenCase must include a FailureTypeFact in "
                    "expected_facts (clause 2 would have nothing to match)"
                )

        return self

    @staticmethod
    def _fact_key(f: Fact) -> str:
        """Canonical string form for set-based disjointness checks."""
        if isinstance(f, OutcomeFact):
            return f"outcome:eq:{f.value}"
        if isinstance(f, FailureTypeFact):
            return f"failure_type:in:{','.join(sorted(f.value))}"
        if isinstance(f, FailureStepFact):
            return f"failure_step:in_range:{f.value[0]},{f.value[1]}"
        raise TypeError(f"unknown fact subtype: {type(f).__name__}")
