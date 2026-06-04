"""LLM-judge for the Trajecta Eval Agent.

This module implements the mechanical foundation for S18 § 2.2 Build 4:
an LLM judge that scores one quality dimension —
``acceptable_eval_case`` — over the Eval Agent's generated
``eval_case_draft`` for each golden-set case, and reports Cohen's κ
against a second annotator (another LLM/prompt pair or a human-labelled
subset).

The Phase 8 production flow is ``backend.app.agent_eval`` first, then a
judge post-step over the exact ``agent_report.json`` and trace directory
that eval run produced. ``eval/judge.py`` remains the standalone
rerun/debug entry point, exposed via A3.4's
``python -m eval.judge --golden … --report … --trace-dir … --out …``.

The final judge task is not "evidence traceability". It is: decide
whether the draft is acceptable as a reusable regression eval case, then
return ``acceptable`` / ``unacceptable`` plus acceptability assertions.

Mechanical prechecks derived from the structured ``expected_facts`` /
``forbidden_facts`` in ``eval/golden.jsonl``:

    1. Verdict match
    2. Failure-type compatibility
    3. Failure-step locality
    4. Expected facts satisfied
    5. No forbidden assertions

A3.2 (this file) adds:

    * Per-EvidenceItem source resolution from the persisted trace +
      storage so the LLM judge never has to call back to Trajecta.
    * A per-case judge payload (``trajectory_id``, ``golden_reference``,
      ``proposed_eval_case``, ``evidence_with_sources``) consumed by the
      LLM call.
    * One env-configured ``acceptable_eval_case`` LLM judge invocation
      (``run_llm_judge``) that A4 reuses for the second provider and
      κ_LLM,LLM rollup.

A3.3 (this file) adds report writers on top of A3.2:

    * ``JudgeCaseReport`` / ``JudgeReport`` dataclasses.
    * ``build_judge_report`` — one-judge per-case rollup from
      ``(trajectory_id, JudgeLLMResult)`` pairs, including ``acceptable_rate``
      and the judge traceability triple (slot, model, prompt_version,
      prompt_sha256).
    * ``write_judge_report`` — emits ``eval/judge_report.json`` and a
      sibling ``eval/judge_report.md`` modelled on
      ``eval/agent_report.md``.

A3.4 (this file) adds the standalone CLI on top of A3.2/A3.3:

    * ``load_agent_report`` — read the ordered ``samples[].trajectory_id``
      list from an ``agent_report.json`` produced by ``agent_eval``.
    * ``run_standalone_judge`` — internal runner that fans out one
      env-configured judge slot across the report's trajectory_ids using the
      A3.2 ``run_llm_judge`` runner + A3.3 ``write_judge_report``
      writers. ``judge_callable`` injection keeps the path mockable in
      tests; the default callable still raises ``NotImplementedError``
      until A4.1 wires real Gemini/OpenAI provider clients.
    * ``build_arg_parser`` + ``main`` — argparse CLI that the
      ``python -m eval.judge`` entry point dispatches to.

A4 extends ``JudgeReport`` to carry both judges plus the κ_LLM,LLM row;
A3.5 adds the ``agent_eval --judge`` post-step that calls
``run_standalone_judge`` end-of-eval against the same artefacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# Make the repo root importable so ``from backend.app.schemas import ...``
# works regardless of which directory the script was launched from.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app import storage  # noqa: E402
from backend.app.schemas import (  # noqa: E402
    AgentTrace,
    EvidenceItem,
    EvalCase,
    FailureMemoryCase,
    FailureStepFact,
    FailureTypeFact,
    GoldenCase,
    OutcomeFact,
    TrajectoryDigest,
    Trajectory,
)

# Default artefact locations match the convention established by A1
# (eval/golden.jsonl committed) and A2 (eval/runs/<stamp>/traces/
# generated locally). The CLI in A3.4 will accept overrides.
DEFAULT_GOLDEN_PATH = _REPO_ROOT / "eval" / "golden.jsonl"
DEFAULT_JUDGE_PROMPTS_ROOT = _REPO_ROOT / "prompts" / "judge"
DEFAULT_REPORT_PATH = _REPO_ROOT / "eval" / "agent_report.json"
DEFAULT_JUDGE_REPORT_PATH = _REPO_ROOT / "eval" / "judge_report.json"

# Tool names that surface retrieval evidence into a trace. Used by
# ``resolve_evidence_source`` to scan for ``failure_memory`` / ``eval_case``
# / ``successful_trajectory`` payloads without re-querying ChromaDB.
_SEARCH_TOOL_NAMES = frozenset(
    {"search_failure_memory", "search_failure_eval_cases", "find_similar_successful_trajectory"}
)

# The five failure-shape fields on an EvalCase. Used to derive whether a
# proposed case is a "success verdict" (all five absent) or a "failure
# verdict" (all five present) — the same XOR enforced by
# ``EvalCase._validate_failure_fields_consistency``.
_FAILURE_SHAPE_FIELDS = (
    "failure_step",
    "failure_type",
    "expected_behavior",
    "actual_behavior",
    "regression_rule",
)


# ---------------------------------------------------------------------------
# Data classes


@dataclass(frozen=True)
class ClauseEvaluation:
    """Result of running judge prechecks against one proposed case.

    Each clause carries one of three values:

      * ``True``  — the clause holds.
      * ``False`` — the clause was checked and failed.
      * ``None``  — the clause does not apply to this golden reference
                    (e.g. clause 2 on a success-shape reference). N/A
                    does **not** count as a failure when aggregating
                    the verdict.

    The final LLM acceptability assertion is left as ``None`` by A3.1;
    A3.2 populates it.
    """

    clause_1_verdict_match: bool | None
    clause_2_failure_type: bool | None
    clause_3_failure_step: bool | None
    clause_4_expected_facts: bool | None
    clause_5_no_forbidden: bool | None
    clause_6_acceptability_assertion: bool | None

    def as_dict(self) -> dict[int, bool | None]:
        return {
            1: self.clause_1_verdict_match,
            2: self.clause_2_failure_type,
            3: self.clause_3_failure_step,
            4: self.clause_4_expected_facts,
            5: self.clause_5_no_forbidden,
            6: self.clause_6_acceptability_assertion,
        }


# ---------------------------------------------------------------------------
# Loaders


def load_golden_cases(path: Path = DEFAULT_GOLDEN_PATH) -> dict[str, GoldenCase]:
    """Load ``eval/golden.jsonl`` into a ``trajectory_id → GoldenCase`` mapping.

    Each row is validated through ``GoldenCase.model_validate``; a stale
    JSONL with the old free-text fact shape raises during validation
    rather than producing silently-wrong judgments. Duplicate trajectory_ids
    across rows raise — the golden set should have one row per run.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"golden set not found at {path}; run `python scripts/build_golden_jsonl.py`"
        )
    cases: dict[str, GoldenCase] = {}
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                case = GoldenCase.model_validate_json(raw)
            except Exception as exc:
                raise ValueError(
                    f"golden case at {path}:{line_no} failed validation: {exc}"
                ) from exc
            if case.input.trajectory_id in cases:
                raise ValueError(
                    f"duplicate trajectory_id {case.input.trajectory_id!r} in {path}; "
                    f"each golden row must be unique"
                )
            cases[case.input.trajectory_id] = case
    return cases


def load_trace(trace_dir: Path, trajectory_id: str) -> AgentTrace:
    """Load a per-sample trace dump produced by ``agent_eval.py --trace-dir``.

    Phase 8 A2 introduced this on-disk format: one ``{trajectory_id}.json`` per
    gradeable sample under ``eval/runs/<stamp>/traces/``. The file
    contents are ``AgentTrace.model_dump_json(indent=2)``.
    """
    path = trace_dir / f"{trajectory_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"trace dump for trajectory_id={trajectory_id!r} not found at {path}; "
            f"re-run `python -m backend.app.agent_eval` to produce it"
        )
    return AgentTrace.model_validate_json(path.read_text(encoding="utf-8"))


def extract_proposed_eval_case(trace: AgentTrace) -> dict[str, Any] | None:
    """Return the ``args`` of the **latest** ``propose_eval_case`` tool
    call in the trace, or ``None`` when the trace did not terminate via
    the terminal tool (budget_exceeded / error terminations leave no
    proposal).

    A multi-turn trace may carry more than one ``propose_eval_case``
    call (the user followed up and the agent re-proposed). The judge
    grades the **latest** draft per ``docs/eval_agent.md`` "Observability"
    invariant: the latest call's args define the current draft.
    """
    latest: dict[str, Any] | None = None
    for ev in trace.events:
        if ev.type == "tool_call" and ev.name == "propose_eval_case":
            latest = ev.args or {}
    return latest


# ---------------------------------------------------------------------------
# Mechanical clauses


def _proposed_is_success(proposed: dict[str, Any]) -> bool:
    """Match ``EvalCase`` success-shape semantics: all five failure
    fields absent.

    A field counts as present iff its value is non-None; an explicit
    ``""`` empty string for ``expected_behavior`` counts as present and
    therefore as failure-shape. This mirrors how
    ``EvalCase._validate_failure_fields_consistency`` decides shape.
    """
    return all(proposed.get(field) is None for field in _FAILURE_SHAPE_FIELDS)


def _check_fact(fact: Any, proposed: dict[str, Any]) -> bool:
    """Does the proposed EvalCase **satisfy** this single ``Fact``?

    The discriminated union (OutcomeFact / FailureTypeFact /
    FailureStepFact) is dispatched by isinstance — the alternative of
    threading the ``field`` literal through string-comparison would
    re-introduce the same regex-parsing fragility we eliminated by
    moving to structured facts.
    """
    if isinstance(fact, OutcomeFact):
        proposed_success = _proposed_is_success(proposed)
        expected_success = fact.value == "success"
        return proposed_success == expected_success
    if isinstance(fact, FailureTypeFact):
        proposed_type = proposed.get("failure_type")
        if not isinstance(proposed_type, str):
            return False
        return proposed_type in set(fact.value)
    if isinstance(fact, FailureStepFact):
        proposed_step = proposed.get("failure_step")
        if not isinstance(proposed_step, int):
            return False
        lo, hi = fact.value
        return lo <= proposed_step <= hi
    raise TypeError(f"unknown Fact subtype: {type(fact).__name__}")


def clause_1_verdict_match(
    golden: GoldenCase, proposed: dict[str, Any]
) -> bool:
    """Clause 1 (M): proposed ``is_success`` matches the
    ``OutcomeFact`` in ``expected_facts``.

    Always applicable — every golden row carries exactly one
    ``OutcomeFact`` in ``expected_facts`` (enforced by
    ``GoldenCase._validate_shape``). Returns a plain bool.
    """
    outcome_fact = next(
        f for f in golden.expected_facts if isinstance(f, OutcomeFact)
    )
    return _check_fact(outcome_fact, proposed)


def clause_2_failure_type_compatibility(
    golden: GoldenCase, proposed: dict[str, Any]
) -> bool | None:
    """Clause 2 (M): proposed ``failure_type`` is in the expected
    multi-label set.

    Returns ``None`` for success-shape references (no ``FailureTypeFact``
    in ``expected_facts``) — the clause does not apply.
    """
    ftype_fact = next(
        (f for f in golden.expected_facts if isinstance(f, FailureTypeFact)),
        None,
    )
    if ftype_fact is None:
        return None
    return _check_fact(ftype_fact, proposed)


def clause_3_failure_step_locality(
    golden: GoldenCase, proposed: dict[str, Any]
) -> bool | None:
    """Clause 3 (M): proposed ``failure_step`` lies in the labelled
    step's ±2 window.

    Returns ``None`` for golden rows without a ``FailureStepFact`` —
    either a success row or a failed row whose triage CSV did not carry
    a ``failure_step`` value.
    """
    fstep_fact = next(
        (f for f in golden.expected_facts if isinstance(f, FailureStepFact)),
        None,
    )
    if fstep_fact is None:
        return None
    return _check_fact(fstep_fact, proposed)


def clause_4_expected_facts_satisfied(
    golden: GoldenCase, proposed: dict[str, Any]
) -> bool:
    """Clause 4 (M): every entry in ``expected_facts`` is satisfied.

    Subsumes clauses 1-3 (each is a single ``expected_facts`` entry).
    The convenience projections in 1-3 surface *which* expected entry
    failed, which 4 by itself cannot — the failed assertion list in
    ``ClauseEvaluation`` therefore distinguishes "verdict wrong" from
    "verdict right but failure_type wrong" from "all expected facts
    fine".
    """
    return all(_check_fact(f, proposed) for f in golden.expected_facts)


def clause_5_no_forbidden_assertions(
    golden: GoldenCase, proposed: dict[str, Any]
) -> bool:
    """Clause 5 (M): no entry in ``forbidden_facts`` is satisfied.

    "Satisfied" here mirrors clause 4's predicate — a forbidden fact is
    violated iff the proposed EvalCase would make it true. E.g. a
    forbidden ``outcome=success`` fact is violated by a success-shape
    proposal.
    """
    return not any(_check_fact(f, proposed) for f in golden.forbidden_facts)


def evaluate_mechanical_clauses(
    golden: GoldenCase, proposed: dict[str, Any]
) -> ClauseEvaluation:
    """Run deterministic prechecks against one (golden, proposed) pair.

    The final LLM acceptability assertion is left ``None``; A3.2 fills
    it in.
    """
    return ClauseEvaluation(
        clause_1_verdict_match=clause_1_verdict_match(golden, proposed),
        clause_2_failure_type=clause_2_failure_type_compatibility(golden, proposed),
        clause_3_failure_step=clause_3_failure_step_locality(golden, proposed),
        clause_4_expected_facts=clause_4_expected_facts_satisfied(golden, proposed),
        clause_5_no_forbidden=clause_5_no_forbidden_assertions(golden, proposed),
        clause_6_acceptability_assertion=None,
    )


# ---------------------------------------------------------------------------
# Verdict aggregation


def aggregate_verdict(
    clauses: ClauseEvaluation,
) -> tuple[str, list[int]]:
    """Collapse a ``ClauseEvaluation`` into a binary verdict and the list
    of failed clause numbers.

    Rules (per ``docs/testing.md`` § LLM Judge):

      * A case is ``acceptable`` iff every checked assertion is ``True``.
      * ``None`` (clause does not apply) does **not** count as a failure.
      * The returned failed list is the sorted list of assertion numbers whose
        value is ``False``; empty when the verdict is ``acceptable``.

    The LLM assertion left as ``None`` — the A3.1-only path — is treated as
    "not yet judged"; the verdict is "acceptable" iff the deterministic
    prechecks all pass. A3.2 always populates this assertion, so a real
    judge run never silently skips it.
    """
    failed: list[int] = []
    for n, value in clauses.as_dict().items():
        if value is False:
            failed.append(n)
    verdict = "acceptable" if not failed else "unacceptable"
    return verdict, sorted(failed)


# ---------------------------------------------------------------------------
# Cohen's κ


def cohens_kappa(a: list[bool], b: list[bool]) -> float:
    """Cohen's κ over two equal-length binary annotation streams.

    Formula (per ``docs/testing.md`` § Cohen's κ pseudocode)::

        p_observed = sum(x == y for x, y in zip(a, b)) / N
        p_expected = p_a_pos * p_b_pos + (1 - p_a_pos) * (1 - p_b_pos)
        kappa      = (p_observed - p_expected) / (1 - p_expected)

    Edge cases the formula does not cover gracefully and how this
    function handles them:

      * ``N == 0`` — there is no agreement to measure. We return ``0.0``
        so a degenerate empty-sample call does not crash the report
        pipeline. The report writer should flag this separately.
      * ``p_expected == 1.0`` — both annotators have unanimous identical
        outputs (e.g. all True or all False). The formula has a divide
        by zero. We return ``1.0`` if observed agreement is also 1.0,
        else ``0.0`` — that is the standard convention for the
        degenerate marginal case.

    The caller is responsible for ensuring ``len(a) == len(b)``; an
    assertion failure here means the report mixed annotators graded on
    disjoint samples, which is an A4/A5 wiring bug and not a bad κ.
    """
    if len(a) != len(b):
        raise ValueError(
            f"cohens_kappa requires equal-length annotation streams; "
            f"got len(a)={len(a)} and len(b)={len(b)}"
        )
    n = len(a)
    if n == 0:
        return 0.0
    p_obs = sum(1 for x, y in zip(a, b) if x == y) / n
    p_a_pos = sum(1 for x in a if x) / n
    p_b_pos = sum(1 for y in b if y) / n
    p_exp = p_a_pos * p_b_pos + (1 - p_a_pos) * (1 - p_b_pos)
    if p_exp >= 1.0 - 1e-12:
        return 1.0 if p_obs >= 1.0 - 1e-12 else 0.0
    return (p_obs - p_exp) / (1 - p_exp)


def disagreement_indices(a: list[bool], b: list[bool]) -> list[int]:
    """Indices where two annotators differ. Powers the disagreement
    analysis section the report writer (A3.2) emits when κ < 0.6."""
    if len(a) != len(b):
        raise ValueError(
            f"disagreement_indices requires equal-length streams; "
            f"got len(a)={len(a)} and len(b)={len(b)}"
        )
    return [i for i, (x, y) in enumerate(zip(a, b)) if x != y]


# ---------------------------------------------------------------------------
# A3.2 — EvidenceItem source resolution
#
# The judge harness pre-resolves each EvidenceItem's source from the
# persisted trace + storage so the LLM never has to call back to Trajecta.
# Per ``docs/testing.md`` § Input shape (per case): the resolved source
# is one of step JSON / failure_memory case / step_detail tool_result /
# eval_case / successful_trajectory record / None (when marked unavailable).


def _items_from_tool_result(event_result: Any) -> list[Any]:
    """Unwrap the ``{"items": [...]}`` envelope produced for list-returning
    tools by ``eval_agent_graph._trace_result_payload``.

    For dict-returning tools (``get_step_detail``, ``get_trajectory``) the payload
    is the dict itself, so we return ``[payload]`` to keep the caller's
    iteration uniform.
    """
    if isinstance(event_result, dict):
        items = event_result.get("items")
        if isinstance(items, list):
            return items
        return [event_result]
    if isinstance(event_result, list):
        return event_result
    return []


def _scan_trace_for_case(
    trace: AgentTrace, *, context_id: str
) -> dict[str, Any] | None:
    """Return the first retrieved case in any search_* tool_result whose
    ``case_id`` matches ``context_id``."""
    for ev in trace.events:
        if ev.type != "tool_result" or ev.name not in _SEARCH_TOOL_NAMES:
            continue
        for item in _items_from_tool_result(ev.result):
            if isinstance(item, dict) and item.get("case_id") == context_id:
                return item
    return None


def _scan_trace_for_successful_trajectory(
    trace: AgentTrace, *, trajectory_id: str
) -> dict[str, Any] | None:
    """Return the first find_similar_successful_trajectory item whose ``trajectory_id``
    matches the EvidenceItem's ``context_id``."""
    for ev in trace.events:
        if ev.type != "tool_result" or ev.name != "find_similar_successful_trajectory":
            continue
        for item in _items_from_tool_result(ev.result):
            if isinstance(item, dict) and item.get("trajectory_id") == trajectory_id:
                return item
    return None


def _scan_trace_for_step_detail_by_seq(
    trace: AgentTrace, *, seq: int
) -> dict[str, Any] | None:
    """Return the tool_result payload at ``seq`` if it is a ``get_step_detail``
    response. ``seq`` here is the EvidenceItem.trace_event_seq the agent
    cited; it should point either at the tool_call or its tool_result.
    Either anchor is acceptable — we walk forward from the cited seq until
    we hit the matching tool_result.
    """
    for ev in trace.events:
        if ev.seq < seq:
            continue
        if ev.type == "tool_result" and ev.name == "get_step_detail":
            return ev.result if isinstance(ev.result, dict) else None
        # Don't walk past a divergent later get_step_detail call.
        if ev.seq > seq and ev.type == "tool_call" and ev.name == "get_step_detail":
            return None
    return None


def resolve_evidence_source(
    item: EvidenceItem,
    *,
    trace: AgentTrace,
    run: Trajectory | None = None,
    digest: TrajectoryDigest | None = None,
    failure_memory_cases: dict[str, FailureMemoryCase] | None = None,
    eval_cases: dict[str, EvalCase] | None = None,
) -> dict[str, Any] | None:
    """Resolve the source content for a single ``EvidenceItem``.

    The judge prompt receives this pre-resolved payload so the LLM never
    has to issue a tool call back into Trajecta. Returns ``None`` when the
    item's ``source == "unavailable"`` (the agent's honest gap) or when
    the source cannot be reconstructed from the supplied trace + storage
    snapshots — in both cases the caller surfaces the gap to the LLM via
    a ``"resolved_source": null`` entry.

    Resolution preference order per source:

      * ``trajectory`` / ``trajectory_digest`` — look the step up by
        ``step_index`` in the supplied ``run`` / ``digest``. The agent may
        also use ``trace_event_seq`` to point at a ``get_trajectory`` tool_result;
        we honour either anchor.
      * ``step_detail_high`` / ``step_detail_low`` — pull the matching
        ``get_step_detail`` tool_result from the trace by
        ``trace_event_seq``. Falling back to ``step_index`` would risk
        confusing two different inspections of the same step.
      * ``failure_memory`` / ``eval_case`` — prefer the trace's
        retrieval tool_result (so the judge sees exactly what the agent
        saw, including any redaction). Fall back to live storage lookups
        only when the trace no longer carries the case.
      * ``successful_trajectory`` — scan ``find_similar_successful_trajectory`` results
        by ``context_id`` (which the agent fills with the comparator's
        ``trajectory_id``).
      * ``unavailable`` — by contract, no source.
    """
    if item.source == "unavailable":
        return None

    if item.source == "trajectory":
        return _resolve_trajectory(item, run=run, trace=trace)

    if item.source == "trajectory_digest":
        return _resolve_digest(item, digest=digest, trace=trace)

    if item.source in ("step_detail_high", "step_detail_low"):
        return _resolve_step_detail(item, trace=trace)

    if item.source == "failure_memory":
        return _resolve_curated_case(
            item, trace=trace, index=failure_memory_cases, loader="failure_memory"
        )

    if item.source == "eval_case":
        return _resolve_curated_case(
            item, trace=trace, index=eval_cases, loader="eval_case"
        )

    if item.source == "successful_trajectory":
        return _resolve_successful_trajectory(item, trace=trace)

    return None


def _resolve_trajectory(
    item: EvidenceItem,
    *,
    run: Trajectory | None,
    trace: AgentTrace,
) -> dict[str, Any] | None:
    if run is not None and item.step_index is not None:
        for step in run.steps:
            if step.index == item.step_index:
                return step.model_dump(mode="json")
    # Fall back to a get_trajectory tool_result if the agent cited one.
    if item.trace_event_seq is not None:
        for ev in trace.events:
            if (
                ev.type == "tool_result"
                and ev.name == "get_trajectory"
                and ev.seq == item.trace_event_seq
                and isinstance(ev.result, dict)
            ):
                return ev.result
    return None


def _resolve_digest(
    item: EvidenceItem,
    *,
    digest: TrajectoryDigest | None,
    trace: AgentTrace,
) -> dict[str, Any] | None:
    if digest is not None and item.step_index is not None:
        for step in digest.steps:
            if step.index == item.step_index:
                return step.model_dump(mode="json")
    # ``get_trajectory`` returns the digest inline as ``trajectory_digest``;
    # honour a trace_event_seq pointing at that payload too.
    if item.trace_event_seq is not None:
        for ev in trace.events:
            if (
                ev.type == "tool_result"
                and ev.name == "get_trajectory"
                and ev.seq == item.trace_event_seq
                and isinstance(ev.result, dict)
            ):
                payload = ev.result.get("trajectory_digest")
                if isinstance(payload, dict):
                    return payload
                return ev.result
    return None


def _resolve_step_detail(
    item: EvidenceItem, *, trace: AgentTrace
) -> dict[str, Any] | None:
    if item.trace_event_seq is None:
        return None
    return _scan_trace_for_step_detail_by_seq(trace, seq=item.trace_event_seq)


def _resolve_curated_case(
    item: EvidenceItem,
    *,
    trace: AgentTrace,
    index: dict[str, Any] | None,
    loader: Literal["failure_memory", "eval_case"],
) -> dict[str, Any] | None:
    if not item.context_id:
        return None
    cached = _scan_trace_for_case(trace, context_id=item.context_id)
    if cached is not None:
        return cached
    if index is not None and item.context_id in index:
        value = index[item.context_id]
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return value
    # Lazy storage fallback. The judge is allowed to use ``storage`` per
    # ``docs/testing.md`` § Input shape, and these two loaders are cheap.
    try:
        if loader == "failure_memory":
            for case in storage.load_failure_memory():
                if case.case_id == item.context_id:
                    return case.model_dump(mode="json")
        else:
            case = storage.load_eval_case(item.context_id)
            if case is not None:
                return case.model_dump(mode="json")
    except Exception:
        # Storage errors are not fatal here — fall through to None so the
        # judge sees an honest gap rather than a stack trace.
        return None
    return None


def _resolve_successful_trajectory(
    item: EvidenceItem, *, trace: AgentTrace
) -> dict[str, Any] | None:
    if item.context_id:
        return _scan_trace_for_successful_trajectory(trace, trajectory_id=item.context_id)
    return None


def build_judge_payload(
    *,
    trajectory_id: str,
    golden: GoldenCase,
    trace: AgentTrace,
    run: Trajectory | None = None,
    digest: TrajectoryDigest | None = None,
    failure_memory_cases: dict[str, FailureMemoryCase] | None = None,
    eval_cases: dict[str, EvalCase] | None = None,
) -> dict[str, Any]:
    """Assemble the per-case payload the LLM judge receives.

    Matches the structure described in ``docs/testing.md`` § Input shape::

        {
          "trajectory_id": "...",
          "golden_reference": {<row from golden.jsonl>},
          "proposed_eval_case": {<args of latest propose_eval_case>} | None,
          "evidence_with_sources": [
            {"evidence": <EvidenceItem>,
             "resolved_source": <step | case | tool_result | None>},
            ...
          ]
        }

    The proposed eval case is the latest ``propose_eval_case`` tool_call's
    args (see ``extract_proposed_eval_case``). When the trace terminated
    via ``budget_exceeded`` / ``error`` and never proposed a case, the
    field is ``None`` and ``evidence_with_sources`` is empty — the judge
    can still mark such a trace ``unacceptable``.
    """
    proposed = extract_proposed_eval_case(trace)
    evidence_with_sources: list[dict[str, Any]] = []
    if proposed is not None:
        raw_evidence = proposed.get("evidence") or []
        if isinstance(raw_evidence, list):
            for raw in raw_evidence:
                item = _coerce_evidence_item(raw)
                if item is None:
                    # Preserve the raw row so the judge can still see
                    # what the agent claimed even if it failed validation.
                    evidence_with_sources.append(
                        {"evidence": raw, "resolved_source": None}
                    )
                    continue
                resolved = resolve_evidence_source(
                    item,
                    trace=trace,
                    run=run,
                    digest=digest,
                    failure_memory_cases=failure_memory_cases,
                    eval_cases=eval_cases,
                )
                evidence_with_sources.append(
                    {
                        "evidence": item.model_dump(mode="json"),
                        "resolved_source": resolved,
                    }
                )

    return {
        "trajectory_id": trajectory_id,
        "golden_reference": golden.model_dump(mode="json"),
        "proposed_eval_case": proposed,
        "evidence_with_sources": evidence_with_sources,
    }


def _coerce_evidence_item(raw: Any) -> EvidenceItem | None:
    if isinstance(raw, EvidenceItem):
        return raw
    if isinstance(raw, dict):
        try:
            return EvidenceItem.model_validate(raw)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# A3.2 — one-provider LLM call foundation
#
# The judge LLM call is structured so the same runner can drive any
# provider-configured model. A3.2 ships the env-driven config dataclass,
# the prompt loader (sha256-stamped), the response parser, and the
# ``run_llm_judge`` entrypoint. A4 reuses this runner for the second
# provider and the κ_LLM,LLM rollup; the real provider clients (Gemini,
# OpenAI) are operator-configured by env at that point.


JudgeSlot = Literal["A", "B"]


@dataclass(frozen=True)
class JudgeConfig:
    """Resolved configuration for one judge slot.

    ``slot`` is "A" (Gemini-compatible by convention) or "B"
    (OpenAI-compatible). ``model`` and ``prompt_version`` come from
    ``TRAJECTA_JUDGE_{slot}_MODEL`` / ``TRAJECTA_JUDGE_{slot}_PROMPT_VERSION``.
    No defaults — the operator picks the concrete model IDs and prompt
    bundle per run (see ``docs/prompt_versioning.md``).
    """

    slot: JudgeSlot
    model: str
    prompt_version: str


def judge_config_from_env(
    slot: JudgeSlot, env: dict[str, str] | None = None
) -> JudgeConfig | None:
    """Read one judge slot's config from the environment.

    Returns ``None`` when either ``TRAJECTA_JUDGE_{slot}_MODEL`` or
    ``TRAJECTA_JUDGE_{slot}_PROMPT_VERSION`` is unset — the caller decides
    whether absence is an error (the production post-step) or a no-op (a
    one-judge debugging rerun).
    """
    src = env if env is not None else os.environ
    model = (src.get(f"TRAJECTA_JUDGE_{slot}_MODEL") or "").strip()
    prompt_version = (src.get(f"TRAJECTA_JUDGE_{slot}_PROMPT_VERSION") or "").strip()
    if not model or not prompt_version:
        return None
    return JudgeConfig(slot=slot, model=model, prompt_version=prompt_version)


def load_judge_prompt(
    version: str, prompts_root: Path | None = None
) -> tuple[str, str]:
    """Return ``(prompt_text, sha256_hex)`` for one committed judge prompt
    bundle. ``prompts/judge/{version}/prompt.md`` is the only file read;
    the sha256 stamp lets the judge report tie a verdict back to the
    exact bytes that produced it (per ``docs/prompt_versioning.md`` §
    Traceability)."""
    root = prompts_root or DEFAULT_JUDGE_PROMPTS_ROOT
    path = root / version / "prompt.md"
    if not path.exists():
        raise FileNotFoundError(
            f"judge prompt bundle not found at {path}; "
            f"create prompts/judge/{version}/prompt.md or rerun with a different "
            f"TRAJECTA_JUDGE_*_PROMPT_VERSION"
        )
    text = path.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, sha


# Type alias for the mockable LLM call. The runner renders the prompt +
# JSON-serialised payload and hands them to this callable; the callable
# returns the raw JSON-string response from the model. Keeping the
# callable signature this narrow is what lets the test suite drive
# ``run_llm_judge`` deterministically without spinning up a real
# provider client.
LLMJudgeCallable = Callable[[str, dict[str, Any]], str]


@dataclass(frozen=True)
class JudgeAssertion:
    """One row in the judge's ``assertions`` list."""

    name: str
    status: Literal["pass", "fail"]
    rationale: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "rationale": self.rationale}


@dataclass(frozen=True)
class JudgeLLMResult:
    """Parsed result of one ``acceptable_eval_case`` LLM judge call.

    ``acceptable`` is the binary stream A4 feeds into Cohen's κ;
    ``assertions`` carries the per-rubric breakdown the report writer
    surfaces; ``model`` / ``prompt_version`` / ``prompt_sha256`` are the
    traceability triple every judge row must carry.
    """

    slot: JudgeSlot
    model: str
    prompt_version: str
    prompt_sha256: str
    verdict: Literal["acceptable", "unacceptable"]
    rationale: str
    assertions: list[JudgeAssertion] = field(default_factory=list)
    raw_response: str = ""

    @property
    def acceptable(self) -> bool:
        return self.verdict == "acceptable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "assertions": [a.as_dict() for a in self.assertions],
        }


def run_llm_judge(
    payload: dict[str, Any],
    config: JudgeConfig,
    *,
    judge_callable: LLMJudgeCallable | None = None,
    prompts_root: Path | None = None,
) -> JudgeLLMResult:
    """Invoke one judge slot against one per-case payload.

    Tests pass ``judge_callable`` to mock the model response; production
    callers either pass a provider-specific callable they build (A4) or
    rely on the default factory below. Either way the parser enforces
    the verdict + assertion shape documented in
    ``docs/testing.md`` § Output shape so the report writer (A3.3) and
    κ runner (A4.3) can trust the field types.
    """
    prompt_text, prompt_sha = load_judge_prompt(config.prompt_version, prompts_root)
    callable_ = judge_callable or _default_judge_callable(config)
    raw_response = callable_(prompt_text, payload)
    parsed = _parse_judge_response(raw_response)
    return JudgeLLMResult(
        slot=config.slot,
        model=config.model,
        prompt_version=config.prompt_version,
        prompt_sha256=prompt_sha,
        verdict=parsed["verdict"],
        rationale=parsed["rationale"],
        assertions=parsed["assertions"],
        raw_response=raw_response,
    )


def _parse_judge_response(raw: str) -> dict[str, Any]:
    """Validate the JSON judge response and coerce it into typed pieces.

    Tolerates a ```` ```json ... ``` ```` code fence because some
    providers wrap structured-output replies that way even when asked
    not to. Everything else — missing ``verdict``, out-of-vocab status,
    non-list assertions — raises ``ValueError`` so a bad model run
    surfaces in the report rather than silently producing an
    "acceptable" verdict from garbage.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        # Strip leading ```json / ``` and trailing ```.
        text = text.lstrip("`")
        # The opening fence may carry a language hint we need to drop.
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
        if text.endswith("```"):
            text = text[:-3].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"judge response is not valid JSON: {exc}; raw={raw!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"judge response must be a JSON object; got {type(parsed).__name__}")
    verdict = parsed.get("verdict")
    if verdict not in ("acceptable", "unacceptable"):
        raise ValueError(
            f"judge verdict must be 'acceptable' or 'unacceptable'; got {verdict!r}"
        )
    rationale = parsed.get("rationale", "")
    if not isinstance(rationale, str):
        raise ValueError(
            f"judge rationale must be a string; got {type(rationale).__name__}"
        )
    raw_assertions = parsed.get("assertions", [])
    if not isinstance(raw_assertions, list):
        raise ValueError(
            f"judge assertions must be a list; got {type(raw_assertions).__name__}"
        )
    assertions: list[JudgeAssertion] = []
    for row in raw_assertions:
        if not isinstance(row, dict):
            raise ValueError(f"judge assertion row must be an object; got {row!r}")
        name = row.get("name")
        status = row.get("status")
        if not isinstance(name, str) or not name:
            raise ValueError(f"judge assertion name must be a non-empty string; got {name!r}")
        if status not in ("pass", "fail"):
            raise ValueError(
                f"judge assertion status must be 'pass' or 'fail'; got {status!r}"
            )
        assertion_rationale = row.get("rationale", "")
        if not isinstance(assertion_rationale, str):
            raise ValueError(
                f"judge assertion rationale must be a string; got {type(assertion_rationale).__name__}"
            )
        assertions.append(
            JudgeAssertion(name=name, status=status, rationale=assertion_rationale)
        )
    return {"verdict": verdict, "rationale": rationale, "assertions": assertions}


# Phase 8 A4.1 — provider env contract.
#
# Both judge slots speak the OpenAI Chat Completions API via the
# ``openai`` SDK. Slot A is documented as Gemini-compatible — operators
# point ``TRAJECTA_JUDGE_A_BASE_URL`` at a Gemini-served OpenAI-compatible
# endpoint and set ``TRAJECTA_JUDGE_A_API_KEY`` to the matching key. Slot
# B is OpenAI-compatible; it may also fall back to the project-wide
# ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` so operators already running
# the eval against OpenAI do not need a second key.
#
# Slot A intentionally does **not** fall back to ``OPENAI_API_KEY`` —
# silently using an OpenAI key for the "Gemini-compatible" slot would
# route the call to the wrong provider and the κ_LLM,LLM number A4.3
# computes would compare two OpenAI judges, not the dual-provider pair
# the S18 deliverable asks for.
_JUDGE_API_KEY_ENV_TMPL = "TRAJECTA_JUDGE_{slot}_API_KEY"
_JUDGE_BASE_URL_ENV_TMPL = "TRAJECTA_JUDGE_{slot}_BASE_URL"


class JudgeProviderError(RuntimeError):
    """Raised when the default judge callable cannot reach its provider.

    Categories covered: missing API key, missing ``openai`` package,
    empty / malformed response content, or any error raised by the
    chat-completions call itself. The standalone CLI and the
    ``agent_eval --judge`` post-step translate this into a per-slot
    "failed" entry rather than letting a stack trace escape — the eval
    itself succeeded; only the judge slot is unhealthy.
    """


def _resolve_provider_creds(
    config: JudgeConfig, *, env: dict[str, str] | None = None
) -> tuple[str, str | None]:
    """Resolve ``(api_key, base_url)`` for one judge slot from env.

    Raises ``JudgeProviderError`` with a slot-tagged message when the
    required API key is missing. ``base_url`` is allowed to be ``None``
    (the openai SDK then uses its built-in default — only meaningful
    for slot B / OpenAI directly).
    """
    src = env if env is not None else os.environ
    key_env = _JUDGE_API_KEY_ENV_TMPL.format(slot=config.slot)
    api_key = (src.get(key_env) or "").strip()
    if not api_key and config.slot == "B":
        api_key = (src.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        fallback_hint = (
            " (or OPENAI_API_KEY)" if config.slot == "B" else ""
        )
        raise JudgeProviderError(
            f"missing API key for judge slot {config.slot!r}: "
            f"set {key_env}{fallback_hint} before running the judge "
            f"(model={config.model!r})"
        )

    base_url_env = _JUDGE_BASE_URL_ENV_TMPL.format(slot=config.slot)
    base_url = (src.get(base_url_env) or "").strip() or None
    if base_url is None and config.slot == "B":
        base_url = (src.get("OPENAI_BASE_URL") or "").strip() or None
    return api_key, base_url


def _build_judge_user_message(payload: dict[str, Any]) -> str:
    """Render the per-case payload as the user-turn content.

    The committed v1_acceptability prompt is the system rubric; the
    user turn carries the structured payload exactly so the LLM has
    no reason to invent fields. Code-fenced JSON keeps the boundary
    explicit even for providers that do not honour
    ``response_format={"type": "json_object"}``.
    """
    return (
        "Case payload (JSON):\n"
        "```json\n"
        + json.dumps(payload, indent=2, sort_keys=True)
        + "\n```"
    )


def _default_judge_callable(
    config: JudgeConfig, *, env: dict[str, str] | None = None
) -> LLMJudgeCallable:
    """Build a real OpenAI-compatible callable for one judge slot.

    Resolution happens **here**, at construction time, so a missing
    API key fails before any provider call is dispatched. The returned
    closure does the per-case work: serialize payload, call
    ``chat.completions.create``, return the raw response text. The
    A3.2 ``_parse_judge_response`` layer is still the only piece
    responsible for verdict / assertion parsing — providers vary
    enough that we keep the parser one level up.

    Tests inject ``env`` to drive a deterministic config without
    touching ``os.environ``; production callers leave it ``None``.
    """
    api_key, base_url = _resolve_provider_creds(config, env=env)
    model_name = config.model
    slot = config.slot

    def _call(prompt_text: str, payload: dict[str, Any]) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise JudgeProviderError(
                f"the `openai` package is required for judge slot {slot!r} "
                f"(model={model_name!r}); install it with `pip install openai`"
            ) from exc

        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url is not None:
            client_kwargs["base_url"] = base_url
        client = OpenAI(**client_kwargs)
        messages = [
            {"role": "system", "content": prompt_text},
            {"role": "user", "content": _build_judge_user_message(payload)},
        ]
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001 — re-raise as JudgeProviderError
            raise JudgeProviderError(
                f"judge slot {slot!r} provider call failed "
                f"(model={model_name!r}): {type(exc).__name__}: {exc}"
            ) from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError) as exc:
            raise JudgeProviderError(
                f"judge slot {slot!r} response had no choices[0].message.content "
                f"(model={model_name!r})"
            ) from exc
        if not isinstance(content, str) or not content.strip():
            raise JudgeProviderError(
                f"judge slot {slot!r} returned empty or non-string content "
                f"(model={model_name!r}, type={type(content).__name__})"
            )
        return content

    return _call


# ---------------------------------------------------------------------------
# A3.3 — report writers
#
# Produces ``eval/judge_report.{json,md}`` from per-case
# ``JudgeLLMResult`` objects for a single judge. A4 extends this to a
# dual-judge report carrying the κ_LLM,LLM row; A3.3 deliberately keeps
# the surface single-judge so the report writer is testable against the
# mockable runner already shipped in A3.2 without provoking real LLM
# calls.
#
# The Markdown layout mirrors ``eval/agent_report.md``: a title, a
# config block, an aggregate block, and a per-case table. The JSON shape
# matches ``docs/testing.md`` § Outputs — one judge stanza, per-case
# entries with assertions, and the aggregate ``acceptable_rate``.


@dataclass(frozen=True)
class JudgeCaseReport:
    """One row in a ``JudgeReport``.

    The fields mirror ``docs/testing.md`` § Output shape (per case);
    ``assertions`` is the LLM judge's per-rubric breakdown so the report
    surfaces which assertions a draft failed — A4's disagreement analysis
    reads the same field when κ < 0.6.
    """

    trajectory_id: str
    verdict: Literal["acceptable", "unacceptable"]
    rationale: str
    assertions: list[JudgeAssertion] = field(default_factory=list)

    @classmethod
    def from_llm_result(
        cls, trajectory_id: str, result: JudgeLLMResult
    ) -> "JudgeCaseReport":
        return cls(
            trajectory_id=trajectory_id,
            verdict=result.verdict,
            rationale=result.rationale,
            assertions=list(result.assertions),
        )

    @property
    def acceptable(self) -> bool:
        return self.verdict == "acceptable"

    def as_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "verdict": self.verdict,
            "rationale": self.rationale,
            "assertions": [a.as_dict() for a in self.assertions],
        }


@dataclass(frozen=True)
class JudgeReport:
    """One-judge per-case rollup.

    Carries the judge traceability triple (slot, model, prompt_version,
    prompt_sha256) so every JSON / Markdown artefact ties verdicts back
    to the exact prompt bytes that produced them — the same contract
    ``AgentTrace`` honours for agent prompts (see
    ``docs/prompt_versioning.md`` § Traceability).
    """

    judge: JudgeConfig
    prompt_sha256: str
    cases: list[JudgeCaseReport] = field(default_factory=list)

    @property
    def sample_size(self) -> int:
        return len(self.cases)

    @property
    def acceptable_count(self) -> int:
        return sum(1 for c in self.cases if c.acceptable)

    @property
    def unacceptable_count(self) -> int:
        return self.sample_size - self.acceptable_count

    @property
    def acceptable_rate(self) -> float:
        """Fraction of cases the judge marked ``acceptable``.

        Returns ``0.0`` when no cases are present — the build path
        already rejects an empty result list, so this guard only matters
        if a caller constructs an empty ``JudgeReport`` directly.
        """
        if self.sample_size == 0:
            return 0.0
        return self.acceptable_count / self.sample_size

    def as_dict(self) -> dict[str, Any]:
        return {
            "judge": {
                "slot": self.judge.slot,
                "model": self.judge.model,
                "prompt_version": self.judge.prompt_version,
                "prompt_sha256": self.prompt_sha256,
            },
            "sample_size": self.sample_size,
            "acceptable_count": self.acceptable_count,
            "unacceptable_count": self.unacceptable_count,
            "acceptable_rate": self.acceptable_rate,
            "cases": [c.as_dict() for c in self.cases],
        }


def build_judge_report(
    judge_results: list[tuple[str, JudgeLLMResult]],
) -> JudgeReport:
    """Roll up ``(trajectory_id, JudgeLLMResult)`` pairs into a one-judge report.

    Validates that every result carries the same slot / model /
    prompt_version / prompt_sha256 — mixing two judges into one report
    would corrupt the aggregate ``acceptable_rate`` and silently break
    A4's κ_LLM,LLM rollup, which depends on per-judge identity.

    Empty input raises: A3.3 reports describe an actual judge run, and
    an empty result set is an operator wiring bug rather than a valid
    "zero acceptable cases" outcome.
    """
    if not judge_results:
        raise ValueError(
            "build_judge_report requires at least one (trajectory_id, JudgeLLMResult) pair; "
            "an empty result set is an operator wiring bug, not a valid report"
        )
    first = judge_results[0][1]
    judge = JudgeConfig(
        slot=first.slot,
        model=first.model,
        prompt_version=first.prompt_version,
    )
    for trajectory_id, result in judge_results:
        if (
            result.slot != first.slot
            or result.model != first.model
            or result.prompt_version != first.prompt_version
            or result.prompt_sha256 != first.prompt_sha256
        ):
            raise ValueError(
                f"build_judge_report received mixed judge identity at trajectory_id={trajectory_id!r}; "
                f"expected slot={first.slot!r} model={first.model!r} "
                f"prompt_version={first.prompt_version!r}; "
                f"got slot={result.slot!r} model={result.model!r} "
                f"prompt_version={result.prompt_version!r}"
            )
    cases = [
        JudgeCaseReport.from_llm_result(trajectory_id, result)
        for trajectory_id, result in judge_results
    ]
    return JudgeReport(judge=judge, prompt_sha256=first.prompt_sha256, cases=cases)


def write_judge_report(
    report: JudgeReport, out_path: Path | str
) -> tuple[Path, Path]:
    """Persist ``report`` as ``out_path`` (JSON) and a sibling ``.md``.

    ``out_path`` is the JSON destination; the Markdown path is
    ``out_path.with_suffix(".md")`` so a caller passing
    ``eval/judge_report.json`` gets ``eval/judge_report.md`` next to it.
    Returns ``(json_path, md_path)``.
    """
    json_path = Path(out_path)
    md_path = json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_render_judge_report_md(report), encoding="utf-8")
    return json_path, md_path


def _render_judge_report_md(report: JudgeReport) -> str:
    """Markdown layout: title → judge config → aggregate → per-case table.

    Mirrors ``eval/agent_report.md`` so a reader who has seen the agent
    quality report can read the judge report without context switching.
    A4 will append the κ_LLM,LLM row and (when needed) a disagreement
    analysis section after the aggregate block.
    """
    lines: list[str] = [
        "# Judge Report",
        "",
        "## Judge configuration",
        "",
        f"- Slot: `{report.judge.slot}`",
        f"- Model: `{report.judge.model}`",
        f"- Prompt version: `{report.judge.prompt_version}`",
        f"- Prompt SHA-256: `{report.prompt_sha256}`",
        "",
        "## Aggregate",
        "",
        f"- Sample count: **{report.sample_size}**",
        f"- Acceptable: {report.acceptable_count}",
        f"- Unacceptable: {report.unacceptable_count}",
        f"- **acceptable_rate: {report.acceptable_rate:.1%}**",
        "",
        "## Per-case verdicts",
        "",
        "| trajectory_id | verdict | rationale |",
        "| --- | --- | --- |",
    ]
    for case in report.cases:
        rationale = _md_one_line(case.rationale, max_len=120)
        lines.append(f"| `{case.trajectory_id}` | `{case.verdict}` | {rationale} |")
    lines.append("")
    return "\n".join(lines)


def _md_one_line(s: str, *, max_len: int) -> str:
    """Collapse a multi-line string into one Markdown-table-safe row.

    Pipes are escaped, newlines collapsed to spaces, and the result is
    truncated with an ellipsis so a long rationale does not visually
    shatter the per-case table.
    """
    flat = (s or "").replace("\r", " ").replace("\n", " ").strip()
    flat = flat.replace("|", "\\|")
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1].rstrip() + "…"


# ---------------------------------------------------------------------------
# A4.3 — κ_LLM,LLM dual-judge agreement rollup
#
# Two one-judge ``JudgeReport``s (one slot A, one slot B) graded over
# the same trajectory_ids in the same order are folded into a single
# ``JudgeAgreementReport`` that carries the per-judge ``acceptable_rate``,
# the binary-verdict Cohen's κ, and the disagreement breakdown that
# ``docs/testing.md`` § "Target and fallback" mandates when κ drops
# below the S18 threshold.
#
# ``selection_policy`` is a free-form string the caller stamps on the
# rollup so a future reader can tell at a glance whether the report
# covers the full 31 gradeable cases (the preferred sample) or a
# deterministic cost-constrained subset — ``docs/testing.md`` §
# "Sample policy" requires the disclosure but does not prescribe the
# exact label, so the field stays opaque to the writer.


# Phase 8 S18 target. Above this, the Markdown writer omits the
# disagreement-analysis section per ``docs/testing.md`` § "Target and
# fallback". Below it, the disagreement section is mandatory and the
# Markdown calls out the κ < 0.6 outcome explicitly so the reader does
# not have to compute the comparison.
_KAPPA_THRESHOLD = 0.6
DEFAULT_SELECTION_POLICY = "full_31_preferred"


def _failed_assertion_names(case: JudgeCaseReport) -> list[str]:
    """Failed assertion names for one case, preserved in author order.

    Used by both the JSON disagreement entries and the Markdown
    disagreement-analysis section. Returning names only (not the full
    rationale) keeps the disagreement table compact; the per-case
    ``cases`` list above still carries full assertion detail when a
    reader wants to drill in."""
    return [a.name for a in case.assertions if a.status == "fail"]


@dataclass(frozen=True)
class JudgeAgreementCase:
    """One paired (Judge A, Judge B) verdict for a single trajectory_id.

    ``judge_a`` and ``judge_b`` are the original single-judge
    ``JudgeCaseReport`` rows verbatim; surfacing them whole keeps the
    paired report self-contained — a downstream consumer never needs
    to cross-reference the two underlying single-judge reports.
    """

    trajectory_id: str
    judge_a: JudgeCaseReport
    judge_b: JudgeCaseReport

    @property
    def agree(self) -> bool:
        return self.judge_a.verdict == self.judge_b.verdict

    @property
    def both_acceptable(self) -> bool:
        return self.judge_a.acceptable and self.judge_b.acceptable

    def as_dict(self) -> dict[str, Any]:
        return {
            "trajectory_id": self.trajectory_id,
            "judge_a": self.judge_a.as_dict(),
            "judge_b": self.judge_b.as_dict(),
            "agree": self.agree,
        }


@dataclass(frozen=True)
class JudgeAgreementReport:
    """Dual-judge rollup carrying κ_LLM,LLM and the disagreement set.

    Each underlying ``JudgeReport`` keeps its own traceability triple
    (slot / model / prompt_version / prompt_sha256). The combined
    report adds the κ row, per-judge acceptable rates already exposed
    by ``JudgeReport.acceptable_rate``, and a list of paired-verdict
    rows in the same order both judges saw.

    ``selection_policy`` is a free-form string the caller stamps so the
    JSON/Markdown clearly state whether the rollup covers the full 31
    gradeable cases or a deterministic cost-constrained subset.
    """

    judge_a: JudgeReport
    judge_b: JudgeReport
    cases: list[JudgeAgreementCase]
    kappa: float
    selection_policy: str = DEFAULT_SELECTION_POLICY

    @property
    def sample_size(self) -> int:
        return len(self.cases)

    @property
    def agreement_count(self) -> int:
        return sum(1 for c in self.cases if c.agree)

    @property
    def disagreement_count(self) -> int:
        return self.sample_size - self.agreement_count

    @property
    def disagreements(self) -> list[JudgeAgreementCase]:
        return [c for c in self.cases if not c.agree]

    @property
    def needs_disagreement_analysis(self) -> bool:
        """``True`` iff κ falls below the S18 target. Drives the
        Markdown writer's "Disagreement Analysis" section."""
        return self.kappa < _KAPPA_THRESHOLD

    def as_dict(self) -> dict[str, Any]:
        def _judge_summary(report: JudgeReport) -> dict[str, Any]:
            return {
                "slot": report.judge.slot,
                "model": report.judge.model,
                "prompt_version": report.judge.prompt_version,
                "prompt_sha256": report.prompt_sha256,
                "acceptable_count": report.acceptable_count,
                "unacceptable_count": report.unacceptable_count,
                "acceptable_rate": report.acceptable_rate,
            }

        return {
            "sample_size": self.sample_size,
            "selection_policy": self.selection_policy,
            "kappa_llm_llm": self.kappa,
            "kappa_threshold": _KAPPA_THRESHOLD,
            "needs_disagreement_analysis": self.needs_disagreement_analysis,
            "agreement_count": self.agreement_count,
            "disagreement_count": self.disagreement_count,
            "judges": {
                "A": _judge_summary(self.judge_a),
                "B": _judge_summary(self.judge_b),
            },
            "cases": [c.as_dict() for c in self.cases],
            "disagreements": [
                {
                    "trajectory_id": c.trajectory_id,
                    "judge_a": {
                        "verdict": c.judge_a.verdict,
                        "rationale": c.judge_a.rationale,
                        "failed_assertions": _failed_assertion_names(c.judge_a),
                    },
                    "judge_b": {
                        "verdict": c.judge_b.verdict,
                        "rationale": c.judge_b.rationale,
                        "failed_assertions": _failed_assertion_names(c.judge_b),
                    },
                }
                for c in self.disagreements
            ],
        }


def build_judge_agreement_report(
    judge_a: JudgeReport,
    judge_b: JudgeReport,
    *,
    selection_policy: str = DEFAULT_SELECTION_POLICY,
) -> JudgeAgreementReport:
    """Fold two single-judge reports into a paired agreement report.

    Invariants enforced here:

      * ``judge_a`` must carry slot ``"A"`` and ``judge_b`` slot
        ``"B"``. Swapping them silently would mis-label the κ row and
        every downstream column.
      * Both reports must list the same trajectory_ids in the same order. The
        agent_eval post-step already grades the two slots over an
        identical fan-out, so a mismatch here is a wiring bug (e.g.
        one slot was rerun against a different golden subset).

    κ is computed via ``cohens_kappa`` over the binary
    ``acceptable`` streams. The threshold check / disagreement
    rendering happens in the writer; this builder is responsible only
    for the data structure.
    """
    if judge_a.judge.slot != "A":
        raise ValueError(
            f"build_judge_agreement_report expected judge_a.slot='A'; "
            f"got {judge_a.judge.slot!r}. The two-judge rollup is "
            f"asymmetric: slot A is Gemini-compatible, slot B is "
            f"OpenAI-compatible (docs/testing.md § Acceptability)."
        )
    if judge_b.judge.slot != "B":
        raise ValueError(
            f"build_judge_agreement_report expected judge_b.slot='B'; "
            f"got {judge_b.judge.slot!r}."
        )

    a_trajectory_ids = [c.trajectory_id for c in judge_a.cases]
    b_trajectory_ids = [c.trajectory_id for c in judge_b.cases]
    if a_trajectory_ids != b_trajectory_ids:
        only_a = sorted(set(a_trajectory_ids) - set(b_trajectory_ids))
        only_b = sorted(set(b_trajectory_ids) - set(a_trajectory_ids))
        order_only_mismatch = set(a_trajectory_ids) == set(b_trajectory_ids)
        detail = (
            "different trajectory_id ordering" if order_only_mismatch
            else f"coverage diff: only_in_A={only_a}, only_in_B={only_b}"
        )
        raise ValueError(
            "build_judge_agreement_report requires both judges to grade "
            "the same trajectory_ids in the same order; "
            f"{detail}. A4.3 κ_LLM,LLM is only meaningful when the two "
            "judge streams are paired one-to-one."
        )

    paired = [
        JudgeAgreementCase(trajectory_id=a.trajectory_id, judge_a=a, judge_b=b)
        for a, b in zip(judge_a.cases, judge_b.cases)
    ]
    a_acceptable = [c.judge_a.acceptable for c in paired]
    b_acceptable = [c.judge_b.acceptable for c in paired]
    kappa = cohens_kappa(a_acceptable, b_acceptable)

    return JudgeAgreementReport(
        judge_a=judge_a,
        judge_b=judge_b,
        cases=paired,
        kappa=kappa,
        selection_policy=selection_policy,
    )


def write_judge_agreement_report(
    report: JudgeAgreementReport, out_path: Path | str
) -> tuple[Path, Path]:
    """Persist ``report`` as ``out_path`` (JSON) and a sibling ``.md``.

    Mirrors ``write_judge_report``'s contract: ``out_path`` is the JSON
    destination; the Markdown path is ``out_path.with_suffix(".md")``.
    The two artefacts together are the Phase 8 § 8.A primary agreement
    deliverable.
    """
    json_path = Path(out_path)
    md_path = json_path.with_suffix(".md")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(report.as_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(_render_judge_agreement_md(report), encoding="utf-8")
    return json_path, md_path


def _render_judge_agreement_md(report: JudgeAgreementReport) -> str:
    """Markdown: title → judges → sample/policy → rates → κ row →
    per-case verdicts → disagreement analysis (conditional).

    Layout mirrors the single-judge Markdown writer so a reader who
    has scanned ``eval/judge_report.md`` for one judge can read the
    dual-judge artefact without context switching.
    """
    a = report.judge_a
    b = report.judge_b
    kappa_status = (
        "PASS — disagreement analysis not required"
        if not report.needs_disagreement_analysis
        else f"BELOW THRESHOLD ({_KAPPA_THRESHOLD}) — see disagreement analysis below"
    )

    lines: list[str] = [
        "# Judge Agreement Report (κ_LLM,LLM)",
        "",
        "## Judges",
        "",
        f"- **Judge A (slot {a.judge.slot})**: model `{a.judge.model}`, "
        f"prompt `{a.judge.prompt_version}` (sha256 `{a.prompt_sha256}`)",
        f"- **Judge B (slot {b.judge.slot})**: model `{b.judge.model}`, "
        f"prompt `{b.judge.prompt_version}` (sha256 `{b.prompt_sha256}`)",
        "",
        "## Sample",
        "",
        f"- Sample size: **{report.sample_size}**",
        f"- Selection policy: `{report.selection_policy}`",
        "",
        "## Acceptable rates",
        "",
        "| Judge | acceptable / total | acceptable_rate |",
        "| --- | --- | --- |",
        f"| A | {a.acceptable_count} / {a.sample_size} | {a.acceptable_rate:.1%} |",
        f"| B | {b.acceptable_count} / {b.sample_size} | {b.acceptable_rate:.1%} |",
        "",
        "## Agreement",
        "",
        f"- **κ_LLM,LLM**: `{report.kappa:.4f}` (target ≥ {_KAPPA_THRESHOLD}) — {kappa_status}",
        f"- Agreement: {report.agreement_count} / {report.sample_size} "
        f"({(report.agreement_count / report.sample_size if report.sample_size else 0):.1%})",
        f"- Disagreements: {report.disagreement_count}",
        "",
        "## Per-case verdicts",
        "",
        "| trajectory_id | Judge A | Judge B | agree |",
        "| --- | --- | --- | --- |",
    ]
    for case in report.cases:
        marker = "✓" if case.agree else "✗"
        lines.append(
            f"| `{case.trajectory_id}` | `{case.judge_a.verdict}` | "
            f"`{case.judge_b.verdict}` | {marker} |"
        )
    lines.append("")

    if report.needs_disagreement_analysis:
        lines.extend(_render_disagreement_section(report))
    return "\n".join(lines) + "\n"


def _render_disagreement_section(report: JudgeAgreementReport) -> list[str]:
    """Per-case disagreement breakdown for the κ < 0.6 fallback path.

    Each disagreement entry lists both judges' verdicts, the failed
    assertion names (one short list per side), and a clipped rationale
    so the reader can immediately see *why* the split happened without
    paging through the full per-case table.
    """
    lines: list[str] = [
        "## Disagreement Analysis",
        "",
        f"κ_LLM,LLM fell below the {_KAPPA_THRESHOLD} target; the cases below "
        "are every run where the two judges produced different verdicts. "
        "Failed assertions are surfaced per side so the operator can "
        "categorise the disagreement (prompt ambiguity vs model behaviour "
        "vs genuinely hard sample). Per `docs/testing.md` § Target and "
        "fallback: a negative result is reported, not relaxed.",
        "",
    ]
    if not report.disagreements:
        # Below-threshold κ with zero disagreements is a degenerate
        # mathematical edge (e.g. unanimous-but-different marginals);
        # docs/testing.md § cohens_kappa documents this as the 0.0
        # fallback. State it explicitly so the section is never empty.
        lines.append(
            "_No paired-verdict disagreements. κ_LLM,LLM reflects a "
            "degenerate marginal distribution rather than per-case "
            "splits — see `cohens_kappa` docstring._"
        )
        lines.append("")
        return lines
    for case in report.disagreements:
        a_failed = _failed_assertion_names(case.judge_a) or ["(none)"]
        b_failed = _failed_assertion_names(case.judge_b) or ["(none)"]
        lines.extend(
            [
                f"### `{case.trajectory_id}`",
                "",
                f"- **Judge A**: `{case.judge_a.verdict}`",
                f"  - Failed assertions: {', '.join(f'`{n}`' for n in a_failed)}",
                f"  - Rationale: {_md_one_line(case.judge_a.rationale, max_len=200)}",
                f"- **Judge B**: `{case.judge_b.verdict}`",
                f"  - Failed assertions: {', '.join(f'`{n}`' for n in b_failed)}",
                f"  - Rationale: {_md_one_line(case.judge_b.rationale, max_len=200)}",
                "",
            ]
        )
    return lines


# ---------------------------------------------------------------------------
# A3.4 — standalone env-configured CLI
#
# The judge post-step that ``agent_eval --judge`` will call in A3.5 needs
# to be runnable as a standalone rerun/debug entry point too: given an
# ``agent_report.json`` produced by an earlier eval run plus the matching
# per-sample trace directory, fan out one env-configured judge slot across
# every gradeable run, write ``judge_report.{json,md}`` to a caller-
# supplied ``--out``, and surface a clean stderr summary.
#
# The internal seam is ``run_standalone_judge`` — ``main`` reads env +
# CLI args and delegates the actual fan-out so the runner stays testable
# with a mocked ``judge_callable``. Real provider clients land with A4.1
# and ``_default_judge_callable`` still raises until then; the CLI fails
# loudly rather than silently producing an empty report.


@dataclass(frozen=True)
class StandaloneJudgeResult:
    """Outcome of one ``run_standalone_judge`` invocation.

    ``report`` is the rolled-up ``JudgeReport`` written to disk; the two
    path fields are the resolved ``(json_path, md_path)`` tuple returned
    by ``write_judge_report``. ``graded_trajectory_ids`` preserves the order
    the runner submitted to the judge so the caller can compare against
    the report's per-case table. ``skipped`` is a reason → trajectory_ids mapping
    so the stderr summary (and A3.5's post-step) can disclose what got
    left out, matching the Phase 8 ``sample_size`` / ``selection_policy``
    disclosure rule in ``docs/testing.md`` § Sample policy.
    """

    report: JudgeReport
    json_path: Path
    md_path: Path
    graded_trajectory_ids: list[str]
    skipped: dict[str, list[str]] = field(default_factory=dict)

    @property
    def skipped_total(self) -> int:
        return sum(len(ids) for ids in self.skipped.values())


def load_agent_report(path: Path) -> list[str]:
    """Read the ordered ``samples[].trajectory_id`` list from an
    ``agent_report.json`` produced by ``backend.app.agent_eval``.

    The report carries a ``samples`` array — one row per gradeable run
    — and a separate ``skipped`` mapping for runs the eval itself
    rejected. A3.4 grades whatever is in ``samples`` (the report's view
    of "gradeable"), preserves its order so reruns are deterministic,
    and surfaces a clean error when the file is missing or malformed.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"agent_report.json not found at {path}; "
            f"run `python -m backend.app.agent_eval` to produce one"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    samples = data.get("samples")
    if not isinstance(samples, list):
        raise ValueError(
            f"{path} is missing a `samples` array; expected a report "
            f"produced by backend.app.agent_eval"
        )
    trajectory_ids: list[str] = []
    for i, row in enumerate(samples):
        if not isinstance(row, dict) or not isinstance(row.get("trajectory_id"), str):
            raise ValueError(
                f"{path}: samples[{i}] is missing a string `trajectory_id` field"
            )
        trajectory_ids.append(row["trajectory_id"])
    return trajectory_ids


def run_standalone_judge(
    *,
    golden_path: Path,
    report_path: Path,
    trace_dir: Path,
    out_path: Path,
    config: JudgeConfig,
    judge_callable: LLMJudgeCallable | None = None,
    sample_size: int | None = None,
    prompts_root: Path | None = None,
) -> StandaloneJudgeResult:
    """Run one judge slot over every gradeable run in an agent report.

    Selection order:

      1. Read ``samples[].trajectory_id`` from ``report_path`` (preserves the
         eval run's per-sample order).
      2. Drop trajectory_ids that have no matching ``GoldenCase`` —
         ``skipped["no_golden"]``.
      3. Drop trajectory_ids whose trace dump is missing —
         ``skipped["missing_trace"]``.
      4. Drop trajectory_ids whose trace did not terminate via
         ``propose_eval_case`` (no draft to grade) —
         ``skipped["no_proposal"]``.
      5. Apply ``sample_size`` (first-N by the report's order).

    The judge is then called once per remaining run with
    ``run_llm_judge(payload, config, judge_callable=…)``. Tests pass a
    mocked callable; production callers either rely on the env-configured
    real provider (wired in A4.1) or feed a callable they built
    themselves. The result is rolled up via ``build_judge_report`` and
    persisted via ``write_judge_report``.

    ``sample_size`` must be a positive integer when supplied. A request
    for more cases than the run produced is honoured silently — the
    grade list shrinks naturally — and the caller sees the skipped
    breakdown.

    Raises ``ValueError`` when no gradeable run remains: an empty judge
    run is an operator wiring bug (wrong report / trace-dir pair) and
    ``build_judge_report`` would reject the empty result list anyway.
    """
    if sample_size is not None and sample_size <= 0:
        raise ValueError(
            f"sample_size must be a positive integer when supplied; "
            f"got {sample_size!r}"
        )

    golden_cases = load_golden_cases(golden_path)
    report_trajectory_ids = load_agent_report(report_path)

    graded: list[tuple[str, JudgeLLMResult]] = []
    graded_trajectory_ids: list[str] = []
    skipped: dict[str, list[str]] = {
        "no_golden": [],
        "missing_trace": [],
        "no_proposal": [],
    }

    for trajectory_id in report_trajectory_ids:
        if sample_size is not None and len(graded) >= sample_size:
            break
        golden = golden_cases.get(trajectory_id)
        if golden is None:
            skipped["no_golden"].append(trajectory_id)
            continue
        try:
            trace = load_trace(trace_dir, trajectory_id)
        except FileNotFoundError:
            skipped["missing_trace"].append(trajectory_id)
            continue
        if extract_proposed_eval_case(trace) is None:
            skipped["no_proposal"].append(trajectory_id)
            continue
        payload = build_judge_payload(trajectory_id=trajectory_id, golden=golden, trace=trace)
        result = run_llm_judge(
            payload,
            config,
            judge_callable=judge_callable,
            prompts_root=prompts_root,
        )
        graded.append((trajectory_id, result))
        graded_trajectory_ids.append(trajectory_id)

    if not graded:
        raise ValueError(
            "no gradeable runs found; "
            f"report={report_path} trace_dir={trace_dir} "
            f"golden={golden_path}; skipped={ {k: len(v) for k, v in skipped.items()} }"
        )

    report = build_judge_report(graded)
    json_path, md_path = write_judge_report(report, out_path)
    return StandaloneJudgeResult(
        report=report,
        json_path=json_path,
        md_path=md_path,
        graded_trajectory_ids=graded_trajectory_ids,
        skipped={k: v for k, v in skipped.items() if v},
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Argument parser for the ``python -m eval.judge`` entry point.

    Mirrors the CLI shape documented in
    ``docs/testing.md`` § Required CLI shape. ``--judge-slot`` defaults
    to ``A`` because the Phase 8 production target lists Judge A
    (Gemini-compatible) first; the rerun/debug path can flip to ``B``
    without further code changes.
    """
    parser = argparse.ArgumentParser(
        prog="python -m eval.judge",
        description=(
            "Rerun the env-configured Trajecta LLM judge against a "
            "previously written agent_report.json + trace directory and "
            "emit eval/judge_report.{json,md} for one judge slot."
        ),
    )
    parser.add_argument(
        "--golden",
        type=Path,
        default=DEFAULT_GOLDEN_PATH,
        help="path to eval/golden.jsonl (default: %(default)s)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help="path to agent_report.json (default: %(default)s)",
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        required=True,
        help="directory of per-sample AgentTrace JSON dumps "
        "(eval/runs/<stamp>/traces/)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_JUDGE_REPORT_PATH,
        help="judge_report.json output path (default: %(default)s); a "
        "sibling .md file is written next to it",
    )
    parser.add_argument(
        "--judge-slot",
        choices=("A", "B"),
        default="A",
        help="which TRAJECTA_JUDGE_<slot>_* env config to use (default: A)",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=None,
        help="cap the number of gradeable runs (deterministic first-N "
        "by report order); the report carries the resulting sample_size",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    judge_callable: LLMJudgeCallable | None = None,
    env: dict[str, str] | None = None,
    prompts_root: Path | None = None,
) -> int:
    """CLI entry point. Returns 0 on success.

    ``judge_callable`` / ``env`` / ``prompts_root`` are test seams so a
    deterministic pytest can exercise the CLI without a real provider
    client or repo-relative env. Production callers (the ``__main__``
    block + A3.5 post-step) leave them ``None`` and inherit
    ``os.environ`` + the committed ``prompts/judge/`` tree.
    """
    args = build_arg_parser().parse_args(argv)
    slot: JudgeSlot = args.judge_slot
    config = judge_config_from_env(slot, env=env)
    if config is None:
        sys.stderr.write(
            f"error: missing TRAJECTA_JUDGE_{slot}_MODEL or "
            f"TRAJECTA_JUDGE_{slot}_PROMPT_VERSION environment variable; "
            f"set both before running `python -m eval.judge` or pass "
            f"--judge-slot for the configured slot\n"
        )
        return 2

    try:
        result = run_standalone_judge(
            golden_path=args.golden,
            report_path=args.report,
            trace_dir=args.trace_dir,
            out_path=args.out,
            config=config,
            judge_callable=judge_callable,
            sample_size=args.sample_size,
            prompts_root=prompts_root,
        )
    except JudgeProviderError as exc:
        # Provider-side failure (missing API key, missing SDK, network
        # transport, empty content). The eval artefacts are still on
        # disk; only this slot is unhealthy. Surface a clean error
        # rather than a stack trace.
        sys.stderr.write(f"error: judge slot {config.slot!r} unavailable: {exc}\n")
        return 3

    sys.stderr.write(
        f"wrote {result.json_path}\n"
        f"wrote {result.md_path}\n"
        f"graded {len(result.graded_trajectory_ids)} cases for judge slot "
        f"{config.slot} (model={config.model}, "
        f"prompt_version={config.prompt_version})\n"
    )
    for reason, ids in result.skipped.items():
        sys.stderr.write(f"skipped[{reason}]={len(ids)}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via tests via main()
    raise SystemExit(main())


__all__ = [
    "ClauseEvaluation",
    "DEFAULT_GOLDEN_PATH",
    "DEFAULT_JUDGE_PROMPTS_ROOT",
    "DEFAULT_JUDGE_REPORT_PATH",
    "DEFAULT_REPORT_PATH",
    "DEFAULT_SELECTION_POLICY",
    "JudgeAgreementCase",
    "JudgeAgreementReport",
    "JudgeAssertion",
    "JudgeCaseReport",
    "JudgeConfig",
    "JudgeLLMResult",
    "JudgeProviderError",
    "JudgeReport",
    "JudgeSlot",
    "LLMJudgeCallable",
    "StandaloneJudgeResult",
    "aggregate_verdict",
    "build_arg_parser",
    "build_judge_agreement_report",
    "build_judge_payload",
    "build_judge_report",
    "clause_1_verdict_match",
    "clause_2_failure_type_compatibility",
    "clause_3_failure_step_locality",
    "clause_4_expected_facts_satisfied",
    "clause_5_no_forbidden_assertions",
    "cohens_kappa",
    "disagreement_indices",
    "evaluate_mechanical_clauses",
    "extract_proposed_eval_case",
    "judge_config_from_env",
    "load_agent_report",
    "load_golden_cases",
    "load_judge_prompt",
    "load_trace",
    "main",
    "resolve_evidence_source",
    "run_llm_judge",
    "run_standalone_judge",
    "write_judge_agreement_report",
    "write_judge_report",
]
