"""Phase 8 A3.1 + A3.2 — tests for the LLM-judge mechanical foundation
plus the A3.2 evidence-resolution / payload-assembly / one-provider LLM
call wiring.

Covers deterministic judge prechecks, Cohen's κ math, the loaders that
bridge ``eval/golden.jsonl`` and the per-sample trace dumps produced by
``backend.app.agent_eval --trace-dir``, the A3.2 evidence-source
resolution helpers, ``build_judge_payload``, and the mockable
``run_llm_judge`` runner. No real Gemini/OpenAI calls are issued from
this test module.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.schemas import (
    AgentTrace,
    AgentTraceEvent,
    EvidenceItem,
    FailureMemoryCase,
    FailureStepFact,
    FailureTypeFact,
    GoldenCase,
    OutcomeFact,
    StepAction,
    StepDigest,
    StepObservation,
    StepResult,
    TrajectoryDigest,
    TrajectoryRun,
    TrajectoryStep,
)
from eval.judge import (
    ClauseEvaluation,
    DEFAULT_SELECTION_POLICY,
    JudgeAgreementCase,
    JudgeAgreementReport,
    JudgeAssertion,
    JudgeCaseReport,
    JudgeConfig,
    JudgeLLMResult,
    JudgeProviderError,
    JudgeReport,
    StandaloneJudgeResult,
    aggregate_verdict,
    build_arg_parser,
    build_judge_agreement_report,
    build_judge_payload,
    build_judge_report,
    clause_1_verdict_match,
    clause_2_failure_type_compatibility,
    clause_3_failure_step_locality,
    clause_4_expected_facts_satisfied,
    clause_5_no_forbidden_assertions,
    cohens_kappa,
    disagreement_indices,
    evaluate_mechanical_clauses,
    extract_proposed_eval_case,
    judge_config_from_env,
    load_agent_report,
    load_golden_cases,
    load_judge_prompt,
    load_trace,
    main as judge_main,
    resolve_evidence_source,
    run_llm_judge,
    run_standalone_judge,
    write_judge_agreement_report,
    write_judge_report,
)
from eval.judge import (  # noqa: E402 — direct test access
    _default_judge_callable,
    _parse_judge_response,
    _resolve_provider_creds,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = REPO_ROOT / "eval" / "golden.jsonl"


# ---------------------------------------------------------------------------
# Fixtures (in-memory; do not depend on eval/golden.jsonl being present)


def _failed_golden(
    *,
    run_id: str = "run_failed",
    failure_types: list[str] | None = None,
    failure_step_range: tuple[int, int] | None = (3, 7),
) -> GoldenCase:
    """A failed-shape golden case with optional step range."""
    failure_types = failure_types or ["missed_constraint"]
    expected: list = [
        OutcomeFact(field="outcome", op="eq", value="failed"),
        FailureTypeFact(field="failure_type", op="in", value=failure_types),
    ]
    if failure_step_range is not None:
        expected.append(
            FailureStepFact(
                field="failure_step", op="in_range", value=failure_step_range
            )
        )
    # forbidden contains the complement failure types plus success.
    other_types = [
        t for t in ("early_terminated", "wrong_target", "wrong_result",
                    "inefficient_search", "missed_constraint")
        if t not in failure_types
    ]
    forbidden: list = [OutcomeFact(field="outcome", op="eq", value="success")]
    if other_types:
        forbidden.append(
            FailureTypeFact(field="failure_type", op="in", value=other_types)
        )
    return GoldenCase(
        input={"run_id": run_id},
        expected_facts=expected,
        forbidden_facts=forbidden,
        tags=["site"],
    )


def _success_golden(run_id: str = "run_success") -> GoldenCase:
    return GoldenCase(
        input={"run_id": run_id},
        expected_facts=[OutcomeFact(field="outcome", op="eq", value="success")],
        forbidden_facts=[OutcomeFact(field="outcome", op="eq", value="failed")],
        tags=["site"],
    )


def _matched_failed_proposal(
    failure_type: str = "missed_constraint", failure_step: int = 5
) -> dict:
    return {
        "run_id": "run_failed",
        "failure_step": failure_step,
        "failure_type": failure_type,
        "expected_behavior": "should satisfy constraint",
        "actual_behavior": "did not satisfy constraint",
        "evidence": [],
        "regression_rule": "verify constraint",
        "retrieved_context_ids": [],
    }


def _matched_success_proposal() -> dict:
    return {
        "run_id": "run_success",
        "failure_step": None,
        "failure_type": None,
        "expected_behavior": None,
        "actual_behavior": None,
        "evidence": [],
        "regression_rule": None,
        "retrieved_context_ids": [],
    }


# ---------------------------------------------------------------------------
# Clause 1 — verdict match


def test_clause_1_failed_ref_matched_failed_proposal_passes() -> None:
    assert clause_1_verdict_match(_failed_golden(), _matched_failed_proposal()) is True


def test_clause_1_failed_ref_success_proposal_fails() -> None:
    assert clause_1_verdict_match(_failed_golden(), _matched_success_proposal()) is False


def test_clause_1_success_ref_matched_success_proposal_passes() -> None:
    assert clause_1_verdict_match(_success_golden(), _matched_success_proposal()) is True


def test_clause_1_success_ref_failed_proposal_fails() -> None:
    assert clause_1_verdict_match(_success_golden(), _matched_failed_proposal()) is False


# ---------------------------------------------------------------------------
# Clause 2 — failure-type compatibility (multi-label OR)


def test_clause_2_failure_type_in_expected_set_passes() -> None:
    golden = _failed_golden(failure_types=["missed_constraint", "early_terminated"])
    proposed = _matched_failed_proposal(failure_type="early_terminated")
    assert clause_2_failure_type_compatibility(golden, proposed) is True


def test_clause_2_failure_type_not_in_expected_set_fails() -> None:
    golden = _failed_golden(failure_types=["missed_constraint"])
    proposed = _matched_failed_proposal(failure_type="wrong_target")
    assert clause_2_failure_type_compatibility(golden, proposed) is False


def test_clause_2_returns_none_for_success_reference() -> None:
    """Success references have no FailureTypeFact — clause is N/A,
    not False. The N/A signal is what aggregate_verdict uses to skip
    the clause when computing failed assertion numbers."""
    golden = _success_golden()
    proposed = _matched_failed_proposal()  # shape mismatch, but clause 2 is N/A
    assert clause_2_failure_type_compatibility(golden, proposed) is None


# ---------------------------------------------------------------------------
# Clause 3 — failure-step locality


def test_clause_3_proposed_step_in_range_passes() -> None:
    golden = _failed_golden(failure_step_range=(3, 7))
    proposed = _matched_failed_proposal(failure_step=5)
    assert clause_3_failure_step_locality(golden, proposed) is True


def test_clause_3_proposed_step_at_boundary_passes() -> None:
    """The range is inclusive on both ends (docs/testing.md "± 2")."""
    golden = _failed_golden(failure_step_range=(3, 7))
    assert clause_3_failure_step_locality(
        golden, _matched_failed_proposal(failure_step=3)
    ) is True
    assert clause_3_failure_step_locality(
        golden, _matched_failed_proposal(failure_step=7)
    ) is True


def test_clause_3_proposed_step_outside_range_fails() -> None:
    golden = _failed_golden(failure_step_range=(3, 7))
    assert clause_3_failure_step_locality(
        golden, _matched_failed_proposal(failure_step=10)
    ) is False


def test_clause_3_returns_none_when_no_failure_step_fact() -> None:
    golden = _failed_golden(failure_step_range=None)
    assert clause_3_failure_step_locality(
        golden, _matched_failed_proposal(failure_step=5)
    ) is None


# ---------------------------------------------------------------------------
# Clause 4 — every expected fact satisfied (the conjunction)


def test_clause_4_all_expected_facts_pass() -> None:
    golden = _failed_golden(failure_step_range=(3, 7))
    proposed = _matched_failed_proposal(failure_step=5)
    assert clause_4_expected_facts_satisfied(golden, proposed) is True


def test_clause_4_fails_when_any_expected_fact_violated() -> None:
    """One out-of-range step is enough to fail clause 4 even though
    clause 1 (outcome) and clause 2 (failure_type) still pass."""
    golden = _failed_golden(failure_step_range=(3, 7))
    proposed = _matched_failed_proposal(failure_step=99)
    assert clause_4_expected_facts_satisfied(golden, proposed) is False


# ---------------------------------------------------------------------------
# Clause 5 — no forbidden fact satisfied


def test_clause_5_no_forbidden_facts_satisfied() -> None:
    golden = _failed_golden()
    proposed = _matched_failed_proposal(failure_type="missed_constraint")
    assert clause_5_no_forbidden_assertions(golden, proposed) is True


def test_clause_5_fails_when_forbidden_failure_type_asserted() -> None:
    """A proposal that asserts a failure_type the golden set explicitly
    forbids (i.e. one of the complement types) trips clause 5. This is
    the most common "agent confused two adjacent failure modes" signal."""
    golden = _failed_golden(failure_types=["missed_constraint"])
    proposed = _matched_failed_proposal(failure_type="wrong_target")
    assert clause_5_no_forbidden_assertions(golden, proposed) is False


def test_clause_5_fails_when_forbidden_outcome_asserted() -> None:
    """For failed references, a success-shape proposal trips the
    forbidden ``outcome=success`` fact."""
    golden = _failed_golden()
    proposed = _matched_success_proposal()
    assert clause_5_no_forbidden_assertions(golden, proposed) is False


# ---------------------------------------------------------------------------
# Aggregate verdict


def test_aggregate_verdict_acceptable_when_all_pass() -> None:
    golden = _failed_golden(failure_step_range=(3, 7))
    proposed = _matched_failed_proposal(failure_step=5)
    clauses = evaluate_mechanical_clauses(golden, proposed)
    verdict, failed = aggregate_verdict(clauses)
    assert verdict == "acceptable"
    assert failed == []


def test_aggregate_verdict_lists_failed_clauses_in_order() -> None:
    """A proposal that gets the verdict right but picks a forbidden
    failure_type should fail clauses 2 + 4 + 5 (clause 4 subsumes 2)."""
    golden = _failed_golden(failure_types=["missed_constraint"])
    proposed = _matched_failed_proposal(failure_type="wrong_target")
    verdict, failed = aggregate_verdict(evaluate_mechanical_clauses(golden, proposed))
    assert verdict == "unacceptable"
    assert failed == [2, 4, 5]


def test_aggregate_verdict_ignores_none_clauses() -> None:
    """N/A clauses do not count as failures. A success reference with a
    matching success proposal should be acceptable even though clauses
    2 and 3 are None."""
    golden = _success_golden()
    proposed = _matched_success_proposal()
    clauses = evaluate_mechanical_clauses(golden, proposed)
    # Clauses 2 and 3 are N/A — they must not appear in failed assertions.
    assert clauses.clause_2_failure_type is None
    assert clauses.clause_3_failure_step is None
    verdict, failed = aggregate_verdict(clauses)
    assert verdict == "acceptable"
    assert failed == []


def test_aggregate_verdict_treats_llm_assertion_none_as_not_failure() -> None:
    """A3.1 leaves the LLM assertion as None (call lives in A3.2). The
    aggregate must still produce a defensible verdict on the
    mechanical clauses alone — otherwise the A3.1 commit cannot be
    independently smoke-tested."""
    clauses = ClauseEvaluation(
        clause_1_verdict_match=True,
        clause_2_failure_type=True,
        clause_3_failure_step=True,
        clause_4_expected_facts=True,
        clause_5_no_forbidden=True,
        clause_6_acceptability_assertion=None,
    )
    verdict, failed = aggregate_verdict(clauses)
    assert verdict == "acceptable"
    assert failed == []


# ---------------------------------------------------------------------------
# Cohen's κ


def test_cohens_kappa_perfect_agreement_is_one() -> None:
    assert cohens_kappa([True, False, True, False], [True, False, True, False]) == 1.0


def test_cohens_kappa_total_disagreement_is_negative() -> None:
    """Two annotators with balanced marginals (50/50 each) but opposite
    labels yield κ = -1 by the formula."""
    k = cohens_kappa([True, False, True, False], [False, True, False, True])
    assert math.isclose(k, -1.0, abs_tol=1e-9)


def test_cohens_kappa_hand_computed_example() -> None:
    """A well-known fixture: 5 cases, annotators agree on 4 of 5,
    p_a = 3/5, p_b = 2/5. Hand-computed κ.

      annotator A: T T T F F   (3 True)
      annotator B: T T F F T   (2 True; agree on cases 0, 1, 3 → 3/5 obs agreement)
                                    ^ cases 2, 4 disagree

      Actually let's recompute carefully:
        idx:        0 1 2 3 4
        A:          T T T F F
        B:          T T F F T
        agree:      ✓ ✓ ✗ ✓ ✗   → 3/5 = 0.6

      p_a_pos = 3/5 = 0.6
      p_b_pos = 3/5 = 0.6
      p_exp   = 0.6*0.6 + 0.4*0.4 = 0.36 + 0.16 = 0.52
      kappa   = (0.6 - 0.52) / (1 - 0.52) = 0.08 / 0.48 = 0.166...
    """
    a = [True, True, True, False, False]
    b = [True, True, False, False, True]
    k = cohens_kappa(a, b)
    assert math.isclose(k, 0.08 / 0.48, abs_tol=1e-9)


def test_cohens_kappa_degenerate_marginals_both_unanimous_same() -> None:
    """When both annotators output all True (or all False), p_expected
    = 1 and the formula divides by zero. Convention: return 1.0 when
    they unanimously agree."""
    assert cohens_kappa([True, True, True], [True, True, True]) == 1.0
    assert cohens_kappa([False, False, False], [False, False, False]) == 1.0


def test_cohens_kappa_degenerate_marginals_unanimous_but_different() -> None:
    """Both annotators are unanimous but on opposite classes — p_expected
    is still 1 but observed agreement is 0. Return 0.0."""
    assert cohens_kappa([True, True, True], [False, False, False]) == 0.0


def test_cohens_kappa_empty_returns_zero() -> None:
    """No samples to score. Return 0 rather than crash so the report
    writer can flag this case explicitly."""
    assert cohens_kappa([], []) == 0.0


def test_cohens_kappa_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="equal-length"):
        cohens_kappa([True, False], [True, False, True])


def test_disagreement_indices_returns_sorted_mismatch_positions() -> None:
    a = [True, False, True, False, True]
    b = [True, True, True, False, False]
    # Disagreements at indices 1 and 4.
    assert disagreement_indices(a, b) == [1, 4]


def test_disagreement_indices_length_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="equal-length"):
        disagreement_indices([True], [True, False])


# ---------------------------------------------------------------------------
# Loaders


def test_load_golden_cases_reads_committed_jsonl() -> None:
    """The repo-committed eval/golden.jsonl must load via the judge's
    loader; this is the smoke test that A1 and A3 schemas have not
    drifted out of sync."""
    if not GOLDEN_PATH.exists():
        pytest.skip("eval/golden.jsonl missing; run scripts/build_golden_jsonl.py")
    cases = load_golden_cases(GOLDEN_PATH)
    assert len(cases) == 35
    # All keys are non-empty hex run_ids.
    for run_id in cases:
        assert run_id and isinstance(run_id, str)


def test_load_golden_cases_rejects_duplicate_run_ids(tmp_path: Path) -> None:
    """Two rows with the same run_id is a builder bug; loader must catch it."""
    row = {
        "input": {"run_id": "duplicate", "intent": "analyze_run"},
        "expected_facts": [{"field": "outcome", "op": "eq", "value": "success"}],
        "forbidden_facts": [{"field": "outcome", "op": "eq", "value": "failed"}],
        "tags": ["x"],
    }
    fake = tmp_path / "g.jsonl"
    fake.write_text(
        json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate run_id"):
        load_golden_cases(fake)


def test_load_trace_round_trips_through_agent_trace_model(tmp_path: Path) -> None:
    """Verify the on-disk format (model_dump_json with indent=2) round-
    trips through ``AgentTrace.model_validate_json`` — i.e. the file
    that A2 writes is in fact loadable by A3.
    """
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        selected_step=None,
        tool_call_count=1,
        turn_count=1,
        terminated_by="propose_eval_case",
        events=[
            AgentTraceEvent(seq=0, turn=0, type="agent_message", message="hi"),
            AgentTraceEvent(
                seq=1,
                turn=0,
                type="tool_call",
                name="propose_eval_case",
                args={
                    "run_id": "run_x",
                    "failure_step": 3,
                    "failure_type": "missed_constraint",
                    "expected_behavior": "x",
                    "actual_behavior": "y",
                    "evidence": [],
                    "regression_rule": "z",
                    "retrieved_context_ids": [],
                },
            ),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    (trace_dir / "run_x.json").write_text(
        trace.model_dump_json(indent=2), encoding="utf-8"
    )
    loaded = load_trace(trace_dir, "run_x")
    assert loaded.run_id == "run_x"
    assert loaded.terminated_by == "propose_eval_case"
    assert loaded.tool_call_count == 1


def test_load_trace_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError, match="trace dump"):
        load_trace(Path("/no/such/dir"), "missing_run")


# ---------------------------------------------------------------------------
# extract_proposed_eval_case


def _trace_with_propose_calls(run_id: str, *, n_calls: int) -> AgentTrace:
    """Build a trace with ``n_calls`` propose_eval_case events to verify
    extract_proposed_eval_case picks the latest."""
    events = []
    seq = 0
    for turn in range(n_calls):
        events.append(
            AgentTraceEvent(
                seq=seq,
                turn=turn,
                type="tool_call",
                name="propose_eval_case",
                args={
                    "run_id": run_id,
                    "failure_step": turn,  # so we can tell calls apart
                    "failure_type": "missed_constraint",
                    "expected_behavior": "x",
                    "actual_behavior": "y",
                    "evidence": [],
                    "regression_rule": "z",
                    "retrieved_context_ids": [],
                },
            )
        )
        seq += 1
    return AgentTrace(
        run_id=run_id,
        user_intent="analyze_run",
        selected_step=None,
        tool_call_count=n_calls,
        turn_count=n_calls,
        terminated_by="propose_eval_case",
        events=events,
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )


def test_extract_proposed_eval_case_returns_latest_propose_call() -> None:
    trace = _trace_with_propose_calls("run_y", n_calls=3)
    args = extract_proposed_eval_case(trace)
    assert args is not None
    # We used `turn` as failure_step so the latest is turn=2 → step 2.
    assert args["failure_step"] == 2


def test_extract_proposed_eval_case_returns_none_when_trace_did_not_terminate() -> None:
    """A trace terminated via budget_exceeded leaves no propose_eval_case
    args. The judge must treat such traces as "no draft to grade"."""
    trace = AgentTrace(
        run_id="run_be",
        user_intent="analyze_run",
        selected_step=None,
        tool_call_count=8,
        turn_count=1,
        terminated_by="budget_exceeded",
        events=[
            AgentTraceEvent(seq=0, turn=0, type="agent_message", message="thinking"),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    assert extract_proposed_eval_case(trace) is None


# ---------------------------------------------------------------------------
# A3.2 — evidence source resolution


def _step(idx: int, *, label: str = "click") -> TrajectoryStep:
    return TrajectoryStep(
        index=idx,
        timestamp=None,
        observation=StepObservation(
            screenshot=f"screenshot_{idx:03d}.png",
            url="https://example.test",
            title=f"step {idx}",
            visible_text=f"label {label}",
        ),
        action=StepAction(type="click", label=label, raw=f"click {label}"),
        result=StepResult(status="unknown"),
    )


def _run(run_id: str = "run_x", n_steps: int = 5) -> TrajectoryRun:
    return TrajectoryRun(
        run_id=run_id,
        task="example task",
        steps=[_step(i + 1) for i in range(n_steps)],
    )


def _digest(run_id: str = "run_x", n_steps: int = 5) -> TrajectoryDigest:
    return TrajectoryDigest(
        run_id=run_id,
        task="example task",
        step_count=n_steps,
        steps=[
            StepDigest(
                index=i + 1,
                action_type="click",
                action_text=f"step {i + 1}",
                action_target=f"target_{i + 1}",
                url="https://example.test",
                title=f"step {i + 1}",
                result_status="unknown",
                coord_validation_status="validated",
                vlm_low_detail_summary=f"summary {i + 1}",
                has_screenshot=True,
            )
            for i in range(n_steps)
        ],
    )


def _empty_trace(run_id: str = "run_x") -> AgentTrace:
    return AgentTrace(
        run_id=run_id,
        user_intent="analyze_run",
        selected_step=None,
        tool_call_count=0,
        turn_count=1,
        terminated_by="propose_eval_case",
        events=[],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )


def test_resolve_unavailable_returns_none() -> None:
    item = EvidenceItem(claim="missing screenshot", source="unavailable")
    assert resolve_evidence_source(item, trace=_empty_trace()) is None


def test_resolve_trajectory_uses_run_step_index() -> None:
    run = _run()
    item = EvidenceItem(
        claim="click happened on step 3",
        source="trajectory",
        run_id="run_x",
        step_index=3,
    )
    resolved = resolve_evidence_source(item, trace=_empty_trace(), run=run)
    assert resolved is not None
    assert resolved["index"] == 3
    assert resolved["observation"]["url"] == "https://example.test"


def test_resolve_trajectory_falls_back_to_get_run_tool_result() -> None:
    """The agent can also cite ``get_run`` via ``trace_event_seq`` — the
    resolver honours that anchor when no run snapshot is supplied."""
    payload = {"run_id": "run_x", "task": "example task", "steps": []}
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        terminated_by="propose_eval_case",
        events=[
            AgentTraceEvent(seq=0, turn=0, type="tool_call", name="get_run", args={"run_id": "run_x"}),
            AgentTraceEvent(seq=1, turn=0, type="tool_result", name="get_run", result=payload),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    item = EvidenceItem(claim="run summary", source="trajectory", trace_event_seq=1)
    assert resolve_evidence_source(item, trace=trace) == payload


def test_resolve_digest_uses_digest_step_index() -> None:
    digest = _digest()
    item = EvidenceItem(
        claim="digest summary step 2",
        source="trajectory_digest",
        step_index=2,
    )
    resolved = resolve_evidence_source(item, trace=_empty_trace(), digest=digest)
    assert resolved is not None
    assert resolved["index"] == 2
    assert resolved["vlm_low_detail_summary"] == "summary 2"


def test_resolve_step_detail_pulls_matching_tool_result() -> None:
    detail_payload = {
        "run_id": "run_x",
        "step_index": 4,
        "has_screenshot": True,
        "vlm_summary": "constraint satisfied",
        "image_detail": "high",
    }
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        terminated_by="propose_eval_case",
        events=[
            AgentTraceEvent(
                seq=0,
                turn=0,
                type="tool_call",
                name="get_step_detail",
                args={"run_id": "run_x", "step_index": 4, "image_detail": "high"},
            ),
            AgentTraceEvent(
                seq=1, turn=0, type="tool_result", name="get_step_detail", result=detail_payload
            ),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    # Agent can cite either the tool_call or tool_result seq — both must resolve.
    for cited_seq in (0, 1):
        item = EvidenceItem(
            claim="step 4 looks fine",
            source="step_detail_high",
            trace_event_seq=cited_seq,
        )
        assert resolve_evidence_source(item, trace=trace) == detail_payload


def test_resolve_failure_memory_prefers_trace_tool_result() -> None:
    """The trace payload reflects what the agent actually saw (including
    any eval-mode redaction). The resolver must prefer it over a fresh
    storage lookup so the judge grades the same view."""
    case_payload = {
        "case_id": "fm_missed_constraint_001",
        "failure_type": "missed_constraint",
        "summary": "agent ignored the labelled constraint",
        "fix_hint": "always re-read the form before submit",
        "tags": [],
        "source_run_id": "earlier_run",
    }
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        terminated_by="propose_eval_case",
        events=[
            AgentTraceEvent(
                seq=0,
                turn=0,
                type="tool_result",
                name="search_failure_memory",
                result={"items": [case_payload]},
            ),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    item = EvidenceItem(
        claim="similar to fm_missed_constraint_001",
        source="failure_memory",
        context_id="fm_missed_constraint_001",
    )
    assert resolve_evidence_source(item, trace=trace) == case_payload


def test_resolve_failure_memory_falls_back_to_index() -> None:
    """When the trace no longer carries the case (e.g. truncated rerun),
    fall back to the provided failure_memory index."""
    case = FailureMemoryCase(
        case_id="fm_missed_constraint_002",
        failure_type="missed_constraint",
        summary="another example",
    )
    item = EvidenceItem(
        claim="prior case",
        source="failure_memory",
        context_id="fm_missed_constraint_002",
    )
    resolved = resolve_evidence_source(
        item,
        trace=_empty_trace(),
        failure_memory_cases={case.case_id: case},
    )
    assert resolved is not None
    assert resolved["case_id"] == "fm_missed_constraint_002"


def test_resolve_successful_run_matches_context_id_against_search_items() -> None:
    item = EvidenceItem(
        claim="similar successful run",
        source="successful_run",
        context_id="success_run_42",
    )
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        terminated_by="propose_eval_case",
        events=[
            AgentTraceEvent(
                seq=0,
                turn=0,
                type="tool_result",
                name="find_similar_successful_run",
                result={
                    "items": [
                        {"run_id": "irrelevant_run", "task": "other"},
                        {"run_id": "success_run_42", "task": "example task"},
                    ]
                },
            ),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    resolved = resolve_evidence_source(item, trace=trace)
    assert resolved == {"run_id": "success_run_42", "task": "example task"}


def test_resolve_returns_none_when_evidence_underspecified() -> None:
    """An EvidenceItem that lacks both the storage anchor (run/digest) and
    the trace anchor (trace_event_seq / context_id) cannot be resolved —
    the judge should see an explicit ``None`` rather than a guessed value."""
    item = EvidenceItem(claim="vague", source="trajectory")
    assert resolve_evidence_source(item, trace=_empty_trace()) is None


# ---------------------------------------------------------------------------
# A3.2 — build_judge_payload


def _propose_event(
    *, seq: int, evidence: list[dict[str, object]], failure_step: int = 3
) -> AgentTraceEvent:
    return AgentTraceEvent(
        seq=seq,
        turn=0,
        type="tool_call",
        name="propose_eval_case",
        args={
            "run_id": "run_x",
            "failure_step": failure_step,
            "failure_type": "missed_constraint",
            "expected_behavior": "should satisfy constraint",
            "actual_behavior": "did not satisfy constraint",
            "evidence": evidence,
            "regression_rule": "verify constraint",
            "retrieved_context_ids": [],
        },
    )


def test_build_judge_payload_assembles_documented_shape() -> None:
    """Matches the input shape documented in docs/testing.md § Input shape."""
    golden = _failed_golden(run_id="run_x")
    detail_payload = {
        "run_id": "run_x",
        "step_index": 3,
        "vlm_summary": "field empty",
        "image_detail": "high",
    }
    propose_evidence = [
        {
            "claim": "field was blank",
            "source": "step_detail_high",
            "run_id": "run_x",
            "step_index": 3,
            "trace_event_seq": 1,
        },
        {"claim": "no screenshot for step 7", "source": "unavailable"},
    ]
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        terminated_by="propose_eval_case",
        events=[
            AgentTraceEvent(
                seq=0,
                turn=0,
                type="tool_call",
                name="get_step_detail",
                args={"run_id": "run_x", "step_index": 3, "image_detail": "high"},
            ),
            AgentTraceEvent(
                seq=1, turn=0, type="tool_result", name="get_step_detail", result=detail_payload
            ),
            _propose_event(seq=2, evidence=propose_evidence, failure_step=3),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    payload = build_judge_payload(run_id="run_x", golden=golden, trace=trace)
    assert payload["run_id"] == "run_x"
    assert payload["golden_reference"]["input"]["run_id"] == "run_x"
    assert payload["proposed_eval_case"]["failure_type"] == "missed_constraint"
    assert len(payload["evidence_with_sources"]) == 2
    first = payload["evidence_with_sources"][0]
    assert first["evidence"]["source"] == "step_detail_high"
    assert first["resolved_source"] == detail_payload
    second = payload["evidence_with_sources"][1]
    assert second["evidence"]["source"] == "unavailable"
    assert second["resolved_source"] is None


def test_build_judge_payload_handles_no_propose_call() -> None:
    """A budget-exceeded trace has no draft — payload still contains the
    golden reference so the judge can mark it unacceptable."""
    golden = _failed_golden(run_id="run_x")
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        tool_call_count=8,
        terminated_by="budget_exceeded",
        events=[
            AgentTraceEvent(seq=0, turn=0, type="agent_message", message="thinking"),
        ],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    payload = build_judge_payload(run_id="run_x", golden=golden, trace=trace)
    assert payload["proposed_eval_case"] is None
    assert payload["evidence_with_sources"] == []


def test_build_judge_payload_preserves_malformed_evidence_rows() -> None:
    """If the agent emits an evidence row that fails EvidenceItem
    validation, the judge still needs to see the raw row — we keep it
    with ``resolved_source = None`` instead of silently dropping it so
    the LLM can flag the violation."""
    golden = _failed_golden(run_id="run_x")
    malformed = {"claim": "missing source field"}
    trace = AgentTrace(
        run_id="run_x",
        user_intent="analyze_run",
        terminated_by="propose_eval_case",
        events=[_propose_event(seq=0, evidence=[malformed])],
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    payload = build_judge_payload(run_id="run_x", golden=golden, trace=trace)
    assert payload["evidence_with_sources"] == [
        {"evidence": malformed, "resolved_source": None}
    ]


# ---------------------------------------------------------------------------
# A3.2 — env-driven judge config + prompt loader


def test_judge_config_from_env_returns_none_when_either_var_missing() -> None:
    assert judge_config_from_env("A", env={}) is None
    assert (
        judge_config_from_env("A", env={"TRAJECTA_JUDGE_A_MODEL": "x"})
        is None
    )
    assert (
        judge_config_from_env(
            "A", env={"TRAJECTA_JUDGE_A_PROMPT_VERSION": "v1"}
        )
        is None
    )


def test_judge_config_from_env_reads_both_slots() -> None:
    env = {
        "TRAJECTA_JUDGE_A_MODEL": "gemini-flash-test",
        "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v1_acceptability",
        "TRAJECTA_JUDGE_B_MODEL": "openai-test",
        "TRAJECTA_JUDGE_B_PROMPT_VERSION": "v1_acceptability",
    }
    a = judge_config_from_env("A", env=env)
    b = judge_config_from_env("B", env=env)
    assert a == JudgeConfig(slot="A", model="gemini-flash-test", prompt_version="v1_acceptability")
    assert b == JudgeConfig(slot="B", model="openai-test", prompt_version="v1_acceptability")


def test_load_judge_prompt_returns_text_and_sha256(tmp_path: Path) -> None:
    """The sha256 lets the report tie a verdict back to the exact prompt
    bytes used (docs/prompt_versioning.md § Traceability)."""
    bundle = tmp_path / "v_test" / "prompt.md"
    bundle.parent.mkdir()
    bundle.write_text("hello judge\n", encoding="utf-8")
    text, sha = load_judge_prompt("v_test", prompts_root=tmp_path)
    assert text == "hello judge\n"
    assert sha == hashlib.sha256("hello judge\n".encode("utf-8")).hexdigest()


def test_load_judge_prompt_missing_bundle_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="judge prompt bundle"):
        load_judge_prompt("v_missing", prompts_root=tmp_path)


def test_load_judge_prompt_reads_committed_v1_acceptability() -> None:
    """Smoke-test the committed prompt bundle so a missing prompt.md in
    the repo trips a test rather than a production no-op."""
    text, sha = load_judge_prompt("v1_acceptability")
    assert "acceptable_eval_case" in text
    assert len(sha) == 64


# ---------------------------------------------------------------------------
# A3.2 — _parse_judge_response


def _good_judge_json(verdict: str = "acceptable") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "rationale": "verdict matches",
            "assertions": [
                {"name": "verdict_alignment", "status": "pass", "rationale": "shape matches"},
                {"name": "evidence_support", "status": "pass", "rationale": "evidence cites step 3"},
            ],
        }
    )


def test_parse_judge_response_happy_path() -> None:
    parsed = _parse_judge_response(_good_judge_json())
    assert parsed["verdict"] == "acceptable"
    assert parsed["rationale"] == "verdict matches"
    assert [a.name for a in parsed["assertions"]] == ["verdict_alignment", "evidence_support"]
    assert all(isinstance(a, JudgeAssertion) for a in parsed["assertions"])


def test_parse_judge_response_strips_code_fence() -> None:
    fenced = "```json\n" + _good_judge_json("unacceptable") + "\n```"
    parsed = _parse_judge_response(fenced)
    assert parsed["verdict"] == "unacceptable"


def test_parse_judge_response_rejects_unknown_verdict() -> None:
    bad = json.dumps({"verdict": "maybe", "rationale": "", "assertions": []})
    with pytest.raises(ValueError, match="verdict"):
        _parse_judge_response(bad)


def test_parse_judge_response_rejects_non_object_payload() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        _parse_judge_response("[\"acceptable\"]")


def test_parse_judge_response_rejects_invalid_assertion_status() -> None:
    bad = json.dumps(
        {
            "verdict": "acceptable",
            "rationale": "",
            "assertions": [{"name": "x", "status": "maybe", "rationale": ""}],
        }
    )
    with pytest.raises(ValueError, match="assertion status"):
        _parse_judge_response(bad)


def test_parse_judge_response_rejects_malformed_json() -> None:
    with pytest.raises(ValueError, match="not valid JSON"):
        _parse_judge_response("not really json {")


# ---------------------------------------------------------------------------
# A3.2 — run_llm_judge with mocked callable


def _judge_prompt_dir(tmp_path: Path, *, body: str = "rubric text") -> Path:
    bundle = tmp_path / "v_acc" / "prompt.md"
    bundle.parent.mkdir()
    bundle.write_text(body, encoding="utf-8")
    return tmp_path


def test_run_llm_judge_returns_typed_result(tmp_path: Path) -> None:
    """A mocked callable replaces the real provider; the runner still
    enforces the documented response shape and stamps slot / model /
    prompt traceability."""
    captured: dict[str, object] = {}

    def fake_judge(prompt: str, payload: dict[str, object]) -> str:
        captured["prompt"] = prompt
        captured["payload_run_id"] = payload["run_id"]
        return _good_judge_json("acceptable")

    config = JudgeConfig(slot="A", model="gemini-flash-test", prompt_version="v_acc")
    payload = {"run_id": "run_x", "golden_reference": {}, "proposed_eval_case": {}, "evidence_with_sources": []}
    result = run_llm_judge(
        payload,
        config,
        judge_callable=fake_judge,
        prompts_root=_judge_prompt_dir(tmp_path),
    )

    assert isinstance(result, JudgeLLMResult)
    assert result.acceptable is True
    assert result.verdict == "acceptable"
    assert result.slot == "A"
    assert result.model == "gemini-flash-test"
    assert result.prompt_version == "v_acc"
    assert result.prompt_sha256 == hashlib.sha256("rubric text".encode("utf-8")).hexdigest()
    assert [a.name for a in result.assertions] == ["verdict_alignment", "evidence_support"]
    # The callable received the prompt text and the payload.
    assert captured["prompt"] == "rubric text"
    assert captured["payload_run_id"] == "run_x"


def test_run_llm_judge_unacceptable_verdict_flips_property(tmp_path: Path) -> None:
    config = JudgeConfig(slot="B", model="openai-test", prompt_version="v_acc")
    payload = {"run_id": "r", "golden_reference": {}, "proposed_eval_case": {}, "evidence_with_sources": []}
    result = run_llm_judge(
        payload,
        config,
        judge_callable=lambda *_args: _good_judge_json("unacceptable"),
        prompts_root=_judge_prompt_dir(tmp_path),
    )
    assert result.acceptable is False
    assert result.verdict == "unacceptable"


def test_run_llm_judge_propagates_parser_failure(tmp_path: Path) -> None:
    """A bad model response must surface as ValueError so the report
    writer (A3.3) can mark the row as judge_error rather than silently
    flipping the verdict."""
    config = JudgeConfig(slot="A", model="gemini-flash-test", prompt_version="v_acc")
    payload = {"run_id": "r", "golden_reference": {}, "proposed_eval_case": {}, "evidence_with_sources": []}
    with pytest.raises(ValueError, match="verdict"):
        run_llm_judge(
            payload,
            config,
            judge_callable=lambda *_args: json.dumps(
                {"verdict": "maybe", "rationale": "", "assertions": []}
            ),
            prompts_root=_judge_prompt_dir(tmp_path),
        )


def test_run_llm_judge_default_callable_raises_when_provider_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A4.1 wires a real provider-backed default callable. Without a
    provider API key in env, the construction-time resolver raises
    ``JudgeProviderError`` so a caller that forgets to pass a mocked
    callable still cannot reach the network — they get an actionable
    error pointing at the missing env var."""
    monkeypatch.delenv("TRAJECTA_JUDGE_A_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = JudgeConfig(slot="A", model="gemini-flash-test", prompt_version="v_acc")
    payload = {"run_id": "r", "golden_reference": {}, "proposed_eval_case": {}, "evidence_with_sources": []}
    with pytest.raises(JudgeProviderError, match="TRAJECTA_JUDGE_A_API_KEY"):
        run_llm_judge(payload, config, prompts_root=_judge_prompt_dir(tmp_path))


def test_run_llm_judge_pipes_payload_into_callable(tmp_path: Path) -> None:
    """The callable signature is the LLM judge contract: prompt + payload
    in, raw JSON string out. The runner must not mutate the payload
    between assembly and the call so A4's second-judge invocation can
    rely on byte-identical inputs across providers."""
    seen: list[dict[str, object]] = []

    def fake(prompt: str, payload: dict[str, object]) -> str:
        seen.append(payload)
        return _good_judge_json("acceptable")

    payload_in = {
        "run_id": "run_x",
        "golden_reference": {"input": {"run_id": "run_x"}},
        "proposed_eval_case": {"failure_type": "missed_constraint"},
        "evidence_with_sources": [
            {"evidence": {"claim": "c", "source": "unavailable"}, "resolved_source": None}
        ],
    }
    config = JudgeConfig(slot="A", model="m", prompt_version="v_acc")
    run_llm_judge(
        payload_in,
        config,
        judge_callable=fake,
        prompts_root=_judge_prompt_dir(tmp_path),
    )
    assert seen == [payload_in]


# ---------------------------------------------------------------------------
# A3.3 — report writers
#
# Single-judge reports only. A4 will extend the writer to a dual-judge
# report carrying the κ_LLM,LLM row; the helpers below produce
# JudgeLLMResult fixtures so the report tests stay deterministic and
# never touch a real provider.


_SHA_A = "a" * 64
_SHA_B = "b" * 64


def _llm_result(
    *,
    slot: str = "A",
    model: str = "judge-a-model",
    prompt_version: str = "v1_acceptability",
    prompt_sha256: str = _SHA_A,
    verdict: str = "acceptable",
    rationale: str = "all checks pass",
    assertions: list[JudgeAssertion] | None = None,
) -> JudgeLLMResult:
    if assertions is None:
        assertions = [
            JudgeAssertion(
                name="verdict_alignment", status="pass", rationale="shape matches"
            )
        ]
    return JudgeLLMResult(
        slot=slot,  # type: ignore[arg-type]  # Literal narrowing in tests
        model=model,
        prompt_version=prompt_version,
        prompt_sha256=prompt_sha256,
        verdict=verdict,  # type: ignore[arg-type]
        rationale=rationale,
        assertions=assertions,
    )


def test_judge_case_report_from_llm_result_copies_fields() -> None:
    result = _llm_result(
        verdict="unacceptable",
        rationale="verdict mismatch",
        assertions=[
            JudgeAssertion(name="verdict_alignment", status="fail", rationale="X"),
            JudgeAssertion(name="evidence_support", status="pass", rationale="Y"),
        ],
    )
    row = JudgeCaseReport.from_llm_result("run_x", result)
    assert row.run_id == "run_x"
    assert row.verdict == "unacceptable"
    assert row.rationale == "verdict mismatch"
    assert row.acceptable is False
    assert [a.status for a in row.assertions] == ["fail", "pass"]


def test_build_judge_report_aggregates_acceptable_rate() -> None:
    """acceptable_rate = acceptable_count / sample_size — N=4 with one
    unacceptable yields 0.75. Tested at the dataclass property level so
    A4's downstream κ rollup does not double-count the boundary."""
    results = [
        ("run_1", _llm_result(verdict="acceptable")),
        ("run_2", _llm_result(verdict="acceptable")),
        ("run_3", _llm_result(verdict="unacceptable", rationale="oops")),
        ("run_4", _llm_result(verdict="acceptable")),
    ]
    report = build_judge_report(results)
    assert report.sample_size == 4
    assert report.acceptable_count == 3
    assert report.unacceptable_count == 1
    assert report.acceptable_rate == 0.75


def test_build_judge_report_preserves_input_order() -> None:
    """The per-case table mirrors the order results were graded in so a
    reader can scan the JSON next to the Markdown."""
    results = [
        ("run_b", _llm_result(verdict="unacceptable")),
        ("run_a", _llm_result(verdict="acceptable")),
        ("run_c", _llm_result(verdict="acceptable")),
    ]
    report = build_judge_report(results)
    assert [c.run_id for c in report.cases] == ["run_b", "run_a", "run_c"]


def test_build_judge_report_rejects_empty_results() -> None:
    """An empty case list is an operator wiring bug, not a valid
    "zero acceptable cases" outcome."""
    with pytest.raises(ValueError, match="at least one"):
        build_judge_report([])


def test_build_judge_report_rejects_mixed_judge_identity() -> None:
    """Mixing two judges into one report would silently corrupt the
    aggregate rate and break A4's κ rollup — the builder must catch it."""
    results = [
        ("run_1", _llm_result(slot="A", model="judge-a-model")),
        ("run_2", _llm_result(slot="B", model="judge-b-model", prompt_sha256=_SHA_B)),
    ]
    with pytest.raises(ValueError, match="mixed judge identity"):
        build_judge_report(results)


def test_build_judge_report_rejects_mixed_prompt_sha() -> None:
    """Same model + slot but the prompt bytes drifted (e.g. an in-flight
    bundle edit) — the prompt_sha256 mismatch is what catches it."""
    results = [
        ("run_1", _llm_result(prompt_sha256=_SHA_A)),
        ("run_2", _llm_result(prompt_sha256=_SHA_B)),
    ]
    with pytest.raises(ValueError, match="mixed judge identity"):
        build_judge_report(results)


def test_write_judge_report_emits_json_shape(tmp_path: Path) -> None:
    """JSON report contains the documented top-level keys (judge config,
    aggregate counts, per-case list) and the per-case assertions."""
    results = [
        (
            "run_1",
            _llm_result(
                verdict="acceptable",
                rationale="all checks pass",
                assertions=[
                    JudgeAssertion(
                        name="verdict_alignment",
                        status="pass",
                        rationale="shape matches",
                    ),
                    JudgeAssertion(
                        name="evidence_support",
                        status="pass",
                        rationale="evidence cites step 3",
                    ),
                ],
            ),
        ),
        (
            "run_2",
            _llm_result(
                verdict="unacceptable",
                rationale="failure_type mismatch",
                assertions=[
                    JudgeAssertion(
                        name="failure_mode_compatibility",
                        status="fail",
                        rationale="wrong_target outside expected set",
                    ),
                ],
            ),
        ),
    ]
    report = build_judge_report(results)
    json_path, md_path = write_judge_report(report, tmp_path / "judge_report.json")
    assert json_path == tmp_path / "judge_report.json"
    assert md_path == tmp_path / "judge_report.md"

    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["judge"] == {
        "slot": "A",
        "model": "judge-a-model",
        "prompt_version": "v1_acceptability",
        "prompt_sha256": _SHA_A,
    }
    assert data["sample_size"] == 2
    assert data["acceptable_count"] == 1
    assert data["unacceptable_count"] == 1
    assert data["acceptable_rate"] == 0.5
    assert [c["run_id"] for c in data["cases"]] == ["run_1", "run_2"]
    assert [c["verdict"] for c in data["cases"]] == ["acceptable", "unacceptable"]
    # Assertions land in JSON exactly as the judge returned them.
    case_1_assertions = data["cases"][0]["assertions"]
    assert [(a["name"], a["status"]) for a in case_1_assertions] == [
        ("verdict_alignment", "pass"),
        ("evidence_support", "pass"),
    ]
    case_2_assertions = data["cases"][1]["assertions"]
    assert case_2_assertions == [
        {
            "name": "failure_mode_compatibility",
            "status": "fail",
            "rationale": "wrong_target outside expected set",
        }
    ]


def test_write_judge_report_md_contains_sample_count_and_rate(tmp_path: Path) -> None:
    """Markdown surfaces the headline numbers and the judge traceability
    triple. A reader scanning the .md should see model, prompt_version,
    prompt_sha256, sample count, acceptable_rate, and per-case verdicts
    without opening the JSON."""
    results = [
        ("run_1", _llm_result(verdict="acceptable")),
        ("run_2", _llm_result(verdict="unacceptable", rationale="bad")),
        ("run_3", _llm_result(verdict="acceptable")),
    ]
    report = build_judge_report(results)
    _, md_path = write_judge_report(report, tmp_path / "judge_report.json")
    md = md_path.read_text(encoding="utf-8")

    assert md.startswith("# Judge Report")
    # Judge traceability triple appears.
    assert "Slot: `A`" in md
    assert "Model: `judge-a-model`" in md
    assert "Prompt version: `v1_acceptability`" in md
    assert f"Prompt SHA-256: `{_SHA_A}`" in md
    # Aggregate block.
    assert "Sample count: **3**" in md
    # 2/3 ≈ 66.7% — formatted to one decimal place.
    assert "acceptable_rate: 66.7%" in md
    # Per-case table contains both verdicts and run_ids.
    assert "| `run_1` | `acceptable` |" in md
    assert "| `run_2` | `unacceptable` |" in md
    assert "| `run_3` | `acceptable` |" in md


def test_write_judge_report_md_escapes_pipes_and_collapses_newlines(
    tmp_path: Path,
) -> None:
    """A rationale containing pipes or newlines must not shatter the
    Markdown table — pipes get backslash-escaped, newlines collapse to
    spaces."""
    results = [
        (
            "run_1",
            _llm_result(
                verdict="unacceptable",
                rationale="line one\nline two | with pipe",
            ),
        )
    ]
    report = build_judge_report(results)
    _, md_path = write_judge_report(report, tmp_path / "judge_report.json")
    md = md_path.read_text(encoding="utf-8")
    # The row should be exactly one line — newline replaced by space, pipe escaped.
    assert "line one line two \\| with pipe" in md
    # And there is no raw embedded newline inside the table row.
    table_row = next(
        line for line in md.splitlines() if line.startswith("| `run_1` |")
    )
    assert "\n" not in table_row


def test_write_judge_report_creates_parent_directory(tmp_path: Path) -> None:
    """A standalone judge run might point ``--out`` at a fresh
    ``eval/runs/{ts}/`` subdir that does not exist yet. The writer
    should create it rather than crash."""
    out_path = tmp_path / "nested" / "deep" / "judge_report.json"
    results = [("run_1", _llm_result())]
    report = build_judge_report(results)
    json_path, md_path = write_judge_report(report, out_path)
    assert json_path.exists()
    assert md_path.exists()


def test_judge_report_acceptable_rate_all_acceptable() -> None:
    """All-acceptable should round-trip cleanly to 100% — the boundary
    that an A4 cost-constrained subset is most likely to hit."""
    results = [("run_1", _llm_result()), ("run_2", _llm_result())]
    report = build_judge_report(results)
    assert report.acceptable_rate == 1.0
    assert report.unacceptable_count == 0


def test_judge_report_acceptable_rate_all_unacceptable() -> None:
    """And the symmetric all-unacceptable case yields 0% — the report
    must not silently drop unacceptable rows."""
    results = [
        ("run_1", _llm_result(verdict="unacceptable")),
        ("run_2", _llm_result(verdict="unacceptable")),
    ]
    report = build_judge_report(results)
    assert report.acceptable_rate == 0.0
    assert report.acceptable_count == 0


# ---------------------------------------------------------------------------
# A3.4 — standalone env-configured CLI
#
# These tests exercise the rerun/debug entry point end-to-end with a mocked
# ``judge_callable`` so the pytest suite stays deterministic and never
# touches a real provider. The CLI ``main`` accepts test-only
# ``judge_callable`` / ``env`` / ``prompts_root`` kwargs so the same path
# can be driven without re-parsing env or copying the committed prompt
# bundle into ``tmp_path``.


def _agent_report_with_samples(run_ids: list[str]) -> dict:
    """A minimal ``agent_report.json``-shaped dict for CLI tests.

    The standalone runner only reads ``samples[].run_id``; the other
    fields the real eval populates (timing, prompt_version, metrics,
    skipped) are irrelevant for the judge fan-out and would just couple
    these tests to the eval-report schema unnecessarily."""
    return {
        "samples": [{"run_id": rid} for rid in run_ids],
        "skipped": {"not_importable": 0, "agent_error": 0},
    }


def _write_trace(trace_dir: Path, run_id: str, *, with_propose: bool = True) -> None:
    """Write a per-sample trace dump in the on-disk format that
    ``agent_eval --trace-dir`` produces. ``with_propose=False`` mimics a
    budget_exceeded / error termination — no draft to grade."""
    if with_propose:
        events = [
            AgentTraceEvent(
                seq=0,
                turn=0,
                type="tool_call",
                name="propose_eval_case",
                args={
                    "run_id": run_id,
                    "failure_step": 5,
                    "failure_type": "missed_constraint",
                    "expected_behavior": "should satisfy constraint",
                    "actual_behavior": "did not satisfy constraint",
                    "evidence": [],
                    "regression_rule": "verify constraint",
                    "retrieved_context_ids": [],
                },
            ),
        ]
        terminated_by = "propose_eval_case"
        tool_call_count = 1
    else:
        events = [AgentTraceEvent(seq=0, turn=0, type="agent_message", message="...")]
        terminated_by = "budget_exceeded"
        tool_call_count = 8
    trace = AgentTrace(
        run_id=run_id,
        user_intent="analyze_run",
        selected_step=None,
        tool_call_count=tool_call_count,
        turn_count=1,
        terminated_by=terminated_by,
        events=events,
        model="mock",
        prompt_version="v5",
        prompt_sha256="0" * 64,
        vlm_model="mock",
    )
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{run_id}.json").write_text(
        trace.model_dump_json(indent=2), encoding="utf-8"
    )


def _write_golden(path: Path, run_ids: list[str]) -> None:
    """Emit a minimal ``golden.jsonl`` covering ``run_ids`` (all failed-shape)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for rid in run_ids:
        rows.append(
            {
                "input": {"run_id": rid, "intent": "analyze_run"},
                "expected_facts": [
                    {"field": "outcome", "op": "eq", "value": "failed"},
                    {
                        "field": "failure_type",
                        "op": "in",
                        "value": ["missed_constraint"],
                    },
                ],
                "forbidden_facts": [
                    {"field": "outcome", "op": "eq", "value": "success"}
                ],
                "tags": ["site", "missed_constraint"],
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _judge_prompts_dir(tmp_path: Path, *, body: str = "rubric") -> Path:
    """Lay out a fake ``prompts/judge/<version>/prompt.md`` tree so the
    CLI tests do not depend on the committed prompt bundle's exact
    bytes."""
    root = tmp_path / "judge_prompts"
    bundle = root / "v_cli" / "prompt.md"
    bundle.parent.mkdir(parents=True)
    bundle.write_text(body, encoding="utf-8")
    return root


def _accept_callable(verdict: str = "acceptable"):
    """A judge callable that ignores its inputs and returns a fixed
    valid JSON response. Lets the CLI tests focus on selection /
    skipping / writer wiring rather than response parsing (covered
    separately above)."""

    def _fake(prompt: str, payload: dict[str, object]) -> str:
        return json.dumps(
            {
                "verdict": verdict,
                "rationale": f"deterministic {verdict}",
                "assertions": [
                    {
                        "name": "verdict_alignment",
                        "status": "pass" if verdict == "acceptable" else "fail",
                        "rationale": "mock",
                    }
                ],
            }
        )

    return _fake


# ---- load_agent_report ----------------------------------------------------


def test_cli_load_agent_report_returns_sample_run_ids(tmp_path: Path) -> None:
    """The runner reads ``samples[].run_id`` in order so subsequent
    ``--sample-size`` selection is deterministic across reruns."""
    report_path = tmp_path / "agent_report.json"
    report_path.write_text(
        json.dumps(_agent_report_with_samples(["run_a", "run_b", "run_c"])),
        encoding="utf-8",
    )
    assert load_agent_report(report_path) == ["run_a", "run_b", "run_c"]


def test_cli_load_agent_report_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError, match="agent_report.json"):
        load_agent_report(Path("/no/such/agent_report.json"))


def test_cli_load_agent_report_rejects_missing_samples_key(tmp_path: Path) -> None:
    """A report without a ``samples`` array is almost certainly the wrong
    file — surface that loudly rather than silently producing an empty
    judge run."""
    path = tmp_path / "agent_report.json"
    path.write_text(json.dumps({"started_at_utc": "x"}), encoding="utf-8")
    with pytest.raises(ValueError, match="samples"):
        load_agent_report(path)


def test_cli_load_agent_report_rejects_non_string_run_id(tmp_path: Path) -> None:
    path = tmp_path / "agent_report.json"
    path.write_text(
        json.dumps({"samples": [{"run_id": 123}]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="run_id"):
        load_agent_report(path)


# ---- run_standalone_judge -------------------------------------------------


def _cli_inputs(
    tmp_path: Path, run_ids: list[str], *, with_propose: bool | dict[str, bool] = True
) -> dict[str, Path]:
    """Lay out a tmp_path with golden + agent_report + trace dir for
    ``run_ids``. ``with_propose`` may be a single bool (applied to all)
    or a per-run mapping so a test can mix gradeable and non-gradeable
    runs in one fixture."""
    if isinstance(with_propose, bool):
        propose_map = {rid: with_propose for rid in run_ids}
    else:
        propose_map = with_propose
    golden_path = tmp_path / "golden.jsonl"
    _write_golden(golden_path, run_ids)
    report_path = tmp_path / "agent_report.json"
    report_path.write_text(
        json.dumps(_agent_report_with_samples(run_ids)), encoding="utf-8"
    )
    trace_dir = tmp_path / "traces"
    for rid, has_propose in propose_map.items():
        _write_trace(trace_dir, rid, with_propose=has_propose)
    out_path = tmp_path / "judge_report.json"
    return {
        "golden": golden_path,
        "report": report_path,
        "trace_dir": trace_dir,
        "out": out_path,
        "prompts_root": _judge_prompts_dir(tmp_path),
    }


def test_cli_run_standalone_judge_grades_each_run(tmp_path: Path) -> None:
    """Happy path: golden + trace + propose_eval_case for every sample.
    The mocked callable runs once per run_id and the rolled-up report
    carries per-case verdicts in submission order."""
    paths = _cli_inputs(tmp_path, ["run_a", "run_b", "run_c"])
    seen_run_ids: list[str] = []

    def fake(prompt: str, payload: dict[str, object]) -> str:
        seen_run_ids.append(payload["run_id"])  # type: ignore[arg-type]
        return _accept_callable("acceptable")(prompt, payload)

    result = run_standalone_judge(
        golden_path=paths["golden"],
        report_path=paths["report"],
        trace_dir=paths["trace_dir"],
        out_path=paths["out"],
        config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
        judge_callable=fake,
        prompts_root=paths["prompts_root"],
    )

    assert isinstance(result, StandaloneJudgeResult)
    assert seen_run_ids == ["run_a", "run_b", "run_c"]
    assert result.graded_run_ids == ["run_a", "run_b", "run_c"]
    assert result.report.sample_size == 3
    assert result.report.acceptable_rate == 1.0
    # Every skipped category was empty so it was pruned from the dict.
    assert result.skipped == {}
    assert result.skipped_total == 0


def test_cli_run_standalone_judge_writes_json_and_md(tmp_path: Path) -> None:
    """The runner persists exactly the same JSON / Markdown layout as
    the A3.3 writer — the CLI's job is wiring, not re-rendering."""
    paths = _cli_inputs(tmp_path, ["run_a"])
    result = run_standalone_judge(
        golden_path=paths["golden"],
        report_path=paths["report"],
        trace_dir=paths["trace_dir"],
        out_path=paths["out"],
        config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
        judge_callable=_accept_callable("acceptable"),
        prompts_root=paths["prompts_root"],
    )
    assert result.json_path == paths["out"]
    assert result.md_path == paths["out"].with_suffix(".md")
    data = json.loads(result.json_path.read_text(encoding="utf-8"))
    assert data["sample_size"] == 1
    assert data["judge"]["slot"] == "A"
    assert data["judge"]["prompt_version"] == "v_cli"
    assert "# Judge Report" in result.md_path.read_text(encoding="utf-8")


def test_cli_run_standalone_judge_respects_sample_size(tmp_path: Path) -> None:
    """``--sample-size`` is a first-N cap by report order — the
    deterministic selection policy the rerun path uses to keep the
    cost-constrained subset reproducible."""
    paths = _cli_inputs(tmp_path, ["run_a", "run_b", "run_c", "run_d"])
    result = run_standalone_judge(
        golden_path=paths["golden"],
        report_path=paths["report"],
        trace_dir=paths["trace_dir"],
        out_path=paths["out"],
        config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
        judge_callable=_accept_callable("acceptable"),
        sample_size=2,
        prompts_root=paths["prompts_root"],
    )
    assert result.graded_run_ids == ["run_a", "run_b"]
    assert result.report.sample_size == 2


def test_cli_run_standalone_judge_sample_size_zero_rejected(tmp_path: Path) -> None:
    """``sample_size`` of 0 / negative is an operator typo — refuse to
    write a meaningless empty report."""
    paths = _cli_inputs(tmp_path, ["run_a"])
    with pytest.raises(ValueError, match="sample_size"):
        run_standalone_judge(
            golden_path=paths["golden"],
            report_path=paths["report"],
            trace_dir=paths["trace_dir"],
            out_path=paths["out"],
            config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
            judge_callable=_accept_callable("acceptable"),
            sample_size=0,
            prompts_root=paths["prompts_root"],
        )


def test_cli_run_standalone_judge_skips_missing_trace(tmp_path: Path) -> None:
    """A run in the agent report whose trace dump was never written
    (or was cleaned up) is skipped under ``missing_trace`` — the
    rerun path must not crash because one run is missing its trace
    file."""
    paths = _cli_inputs(tmp_path, ["run_a", "run_b"])
    # Delete one of the two trace dumps so the runner can see it as missing.
    (paths["trace_dir"] / "run_b.json").unlink()

    result = run_standalone_judge(
        golden_path=paths["golden"],
        report_path=paths["report"],
        trace_dir=paths["trace_dir"],
        out_path=paths["out"],
        config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
        judge_callable=_accept_callable("acceptable"),
        prompts_root=paths["prompts_root"],
    )
    assert result.graded_run_ids == ["run_a"]
    assert result.skipped == {"missing_trace": ["run_b"]}
    assert result.report.sample_size == 1


def test_cli_run_standalone_judge_skips_when_no_proposal(tmp_path: Path) -> None:
    """A trace that terminated via ``budget_exceeded`` carries no
    ``propose_eval_case`` args. Without a draft to grade we skip — the
    judge would just be asked to grade ``None`` and call it
    unacceptable, which adds no signal but burns provider budget."""
    paths = _cli_inputs(
        tmp_path,
        ["run_a", "run_b"],
        with_propose={"run_a": True, "run_b": False},
    )
    result = run_standalone_judge(
        golden_path=paths["golden"],
        report_path=paths["report"],
        trace_dir=paths["trace_dir"],
        out_path=paths["out"],
        config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
        judge_callable=_accept_callable("acceptable"),
        prompts_root=paths["prompts_root"],
    )
    assert result.graded_run_ids == ["run_a"]
    assert result.skipped == {"no_proposal": ["run_b"]}


def test_cli_run_standalone_judge_skips_when_no_golden(tmp_path: Path) -> None:
    """Defensive: a run in the agent report that the current golden set
    no longer covers is skipped under ``no_golden`` rather than raising.
    This protects reruns against an out-of-date report+golden pairing."""
    paths = _cli_inputs(tmp_path, ["run_a"])
    # Add a second sample to the report that the golden does not cover.
    paths["report"].write_text(
        json.dumps(_agent_report_with_samples(["run_a", "run_x"])),
        encoding="utf-8",
    )
    _write_trace(paths["trace_dir"], "run_x", with_propose=True)

    result = run_standalone_judge(
        golden_path=paths["golden"],
        report_path=paths["report"],
        trace_dir=paths["trace_dir"],
        out_path=paths["out"],
        config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
        judge_callable=_accept_callable("acceptable"),
        prompts_root=paths["prompts_root"],
    )
    assert result.graded_run_ids == ["run_a"]
    assert result.skipped == {"no_golden": ["run_x"]}


def test_cli_run_standalone_judge_empty_result_raises(tmp_path: Path) -> None:
    """All runs skipped → no judge calls → no report. Refuse to write
    an empty report so a wrong --report / --trace-dir pair surfaces as
    an error rather than a misleading 0-row report."""
    paths = _cli_inputs(
        tmp_path,
        ["run_a", "run_b"],
        with_propose={"run_a": False, "run_b": False},
    )
    with pytest.raises(ValueError, match="no gradeable"):
        run_standalone_judge(
            golden_path=paths["golden"],
            report_path=paths["report"],
            trace_dir=paths["trace_dir"],
            out_path=paths["out"],
            config=JudgeConfig(slot="A", model="m", prompt_version="v_cli"),
            judge_callable=_accept_callable("acceptable"),
            prompts_root=paths["prompts_root"],
        )


# ---- argparse + main() ----------------------------------------------------


def test_cli_build_arg_parser_accepts_documented_flags(tmp_path: Path) -> None:
    """Documented CLI shape: --golden / --report / --trace-dir / --out /
    --judge-slot / --sample-size. The default values surface on parse so
    a regression in the parser shape is caught here rather than at
    rerun time."""
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--golden",
            str(tmp_path / "g.jsonl"),
            "--report",
            str(tmp_path / "r.json"),
            "--trace-dir",
            str(tmp_path / "traces"),
            "--out",
            str(tmp_path / "j.json"),
            "--judge-slot",
            "B",
            "--sample-size",
            "5",
        ]
    )
    assert args.golden == tmp_path / "g.jsonl"
    assert args.report == tmp_path / "r.json"
    assert args.trace_dir == tmp_path / "traces"
    assert args.out == tmp_path / "j.json"
    assert args.judge_slot == "B"
    assert args.sample_size == 5


def test_cli_build_arg_parser_defaults_to_judge_slot_a() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--trace-dir", "/tmp/traces"])
    assert args.judge_slot == "A"
    assert args.sample_size is None


def test_cli_main_with_env_and_mocked_callable(tmp_path: Path) -> None:
    """End-to-end ``main([...])`` exercise with mocked env + callable:
    no real provider, no real env vars, no committed prompt bundle
    dependency. Verifies the CLI wiring (env read → config → runner →
    writer) lines up."""
    paths = _cli_inputs(tmp_path, ["run_a", "run_b"])
    argv = [
        "--golden",
        str(paths["golden"]),
        "--report",
        str(paths["report"]),
        "--trace-dir",
        str(paths["trace_dir"]),
        "--out",
        str(paths["out"]),
        "--judge-slot",
        "A",
    ]
    env = {
        "TRAJECTA_JUDGE_A_MODEL": "gemini-flash-mock",
        "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_cli",
    }
    rc = judge_main(
        argv,
        judge_callable=_accept_callable("acceptable"),
        env=env,
        prompts_root=paths["prompts_root"],
    )
    assert rc == 0
    data = json.loads(paths["out"].read_text(encoding="utf-8"))
    assert data["sample_size"] == 2
    assert data["judge"]["model"] == "gemini-flash-mock"
    assert data["judge"]["prompt_version"] == "v_cli"
    assert paths["out"].with_suffix(".md").exists()


def test_cli_main_returns_nonzero_when_env_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without ``TRAJECTA_JUDGE_<slot>_MODEL`` and
    ``..._PROMPT_VERSION`` set, the CLI must fail loudly — silently
    falling back to "no-op" would leave the operator with a missing
    judge report and no error."""
    paths = _cli_inputs(tmp_path, ["run_a"])
    argv = [
        "--golden",
        str(paths["golden"]),
        "--report",
        str(paths["report"]),
        "--trace-dir",
        str(paths["trace_dir"]),
        "--out",
        str(paths["out"]),
    ]
    rc = judge_main(argv, env={}, prompts_root=paths["prompts_root"])
    assert rc != 0
    stderr = capsys.readouterr().err
    assert "TRAJECTA_JUDGE_A_MODEL" in stderr or "TRAJECTA_JUDGE_A_PROMPT_VERSION" in stderr


def test_cli_main_default_callable_without_api_key_returns_provider_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A4.1: ``main`` without a mocked callable resolves to
    ``_default_judge_callable``, which builds an OpenAI-compatible
    client. When the slot's API key env is absent the resolver raises
    ``JudgeProviderError`` and the CLI surfaces a clean exit code 3
    with an actionable message — not a stack trace."""
    monkeypatch.delenv("TRAJECTA_JUDGE_A_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    paths = _cli_inputs(tmp_path, ["run_a"])
    argv = [
        "--golden",
        str(paths["golden"]),
        "--report",
        str(paths["report"]),
        "--trace-dir",
        str(paths["trace_dir"]),
        "--out",
        str(paths["out"]),
    ]
    env = {
        "TRAJECTA_JUDGE_A_MODEL": "gemini-flash-mock",
        "TRAJECTA_JUDGE_A_PROMPT_VERSION": "v_cli",
    }
    rc = judge_main(argv, env=env, prompts_root=paths["prompts_root"])
    assert rc == 3
    stderr = capsys.readouterr().err
    assert "TRAJECTA_JUDGE_A_API_KEY" in stderr
    assert "'A'" in stderr or "slot 'A'" in stderr


# ---------------------------------------------------------------------------
# A4.1 — env-driven default judge callable
#
# The default callable wraps an OpenAI-compatible chat-completions
# client. These tests cover the resolver (env contract) and the call
# layer (request shape + response handling) with a mocked
# ``openai.OpenAI`` constructor — no real HTTP, no network.


def _judge_config(slot: str = "A", model: str = "judge-model") -> JudgeConfig:
    return JudgeConfig(slot=slot, model=model, prompt_version="v_acc")  # type: ignore[arg-type]


class _FakeChatMessage:
    def __init__(self, content: object) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: object) -> None:
        self.message = _FakeChatMessage(content)


class _FakeChatCompletion:
    def __init__(self, content: object) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeChatCompletions:
    def __init__(self, owner: "_FakeOpenAI") -> None:
        self._owner = owner

    def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if self._owner.exc is not None:
            raise self._owner.exc
        return _FakeChatCompletion(self._owner.content)


class _FakeChat:
    def __init__(self, owner: "_FakeOpenAI") -> None:
        self.completions = _FakeChatCompletions(owner)


class _FakeOpenAI:
    """Stand-in for ``openai.OpenAI`` that records construction kwargs
    and exposes a controllable ``chat.completions.create`` response.

    Patching ``openai.OpenAI`` with a factory that returns one of these
    keeps the entire provider call path deterministic and offline."""

    def __init__(
        self, *, content: object = '{"verdict": "acceptable", "rationale": "", "assertions": []}',
        exc: BaseException | None = None,
    ) -> None:
        self.construction_calls: list[dict[str, object]] = []
        self.calls: list[dict[str, object]] = []
        self.content = content
        self.exc = exc
        self.chat = _FakeChat(self)

    def factory(self) -> "type[_FakeOpenAI]":
        """Return a callable usable as the ``openai.OpenAI`` constructor.

        We can't just patch ``openai.OpenAI`` with ``self`` because the
        production code instantiates it with kwargs. Instead, hand back
        a closure that records kwargs then returns ``self``."""
        fake = self

        class _Constructor:
            def __init__(self, **kwargs):
                fake.construction_calls.append(kwargs)
                # Mirror the OpenAI client surface
                self.chat = fake.chat

        return _Constructor  # type: ignore[return-value]


# ---- _resolve_provider_creds ---------------------------------------------


def test_resolve_provider_creds_slot_A_requires_explicit_key() -> None:
    """Slot A is Gemini-compatible — silently using OPENAI_API_KEY would
    route the call to the wrong provider. The resolver must demand
    TRAJECTA_JUDGE_A_API_KEY explicitly."""
    with pytest.raises(JudgeProviderError, match="TRAJECTA_JUDGE_A_API_KEY"):
        _resolve_provider_creds(
            _judge_config(slot="A"),
            env={"OPENAI_API_KEY": "should-not-be-used"},
        )


def test_resolve_provider_creds_slot_A_message_excludes_openai_fallback_hint() -> None:
    """Defence against future regression: the error for slot A must NOT
    list OPENAI_API_KEY as a fallback — that would mislead operators
    into setting the wrong key."""
    try:
        _resolve_provider_creds(_judge_config(slot="A"), env={})
    except JudgeProviderError as exc:
        assert "OPENAI_API_KEY" not in str(exc)
    else:
        pytest.fail("expected JudgeProviderError")


def test_resolve_provider_creds_slot_A_reads_slot_specific_env() -> None:
    api_key, base_url = _resolve_provider_creds(
        _judge_config(slot="A"),
        env={
            "TRAJECTA_JUDGE_A_API_KEY": "gemini-key",
            "TRAJECTA_JUDGE_A_BASE_URL": "https://gemini.example/v1",
        },
    )
    assert api_key == "gemini-key"
    assert base_url == "https://gemini.example/v1"


def test_resolve_provider_creds_slot_B_prefers_slot_specific_key() -> None:
    """When both TRAJECTA_JUDGE_B_API_KEY and OPENAI_API_KEY are set,
    the slot-specific one wins — the operator's intent to call a
    different OpenAI-compatible endpoint for the judge must not be
    silently overridden by the agent's existing OPENAI_API_KEY."""
    api_key, base_url = _resolve_provider_creds(
        _judge_config(slot="B"),
        env={
            "TRAJECTA_JUDGE_B_API_KEY": "explicit-judge-b",
            "OPENAI_API_KEY": "ambient-agent-key",
        },
    )
    assert api_key == "explicit-judge-b"
    assert base_url is None


def test_resolve_provider_creds_slot_B_falls_back_to_openai_env() -> None:
    """The documented convenience for operators already running the
    agent against OpenAI: slot B without its own key reuses
    OPENAI_API_KEY / OPENAI_BASE_URL."""
    api_key, base_url = _resolve_provider_creds(
        _judge_config(slot="B"),
        env={
            "OPENAI_API_KEY": "ambient-openai",
            "OPENAI_BASE_URL": "https://api.openai.test/v1",
        },
    )
    assert api_key == "ambient-openai"
    assert base_url == "https://api.openai.test/v1"


def test_resolve_provider_creds_slot_B_error_mentions_openai_fallback() -> None:
    """For slot B, the error message should advertise the OPENAI_API_KEY
    fallback so operators know they have two valid ways to wire it."""
    try:
        _resolve_provider_creds(_judge_config(slot="B"), env={})
    except JudgeProviderError as exc:
        assert "TRAJECTA_JUDGE_B_API_KEY" in str(exc)
        assert "OPENAI_API_KEY" in str(exc)
    else:
        pytest.fail("expected JudgeProviderError")


def test_resolve_provider_creds_blank_strings_count_as_missing() -> None:
    """Whitespace-only env values are a common operator typo
    (`export TRAJECTA_JUDGE_A_API_KEY=` leaves it set to ""). Treat
    blank as missing rather than letting it through to the provider."""
    with pytest.raises(JudgeProviderError):
        _resolve_provider_creds(
            _judge_config(slot="A"),
            env={"TRAJECTA_JUDGE_A_API_KEY": "   "},
        )


# ---- _default_judge_callable (provider call path) ------------------------


def _patch_openai(fake: _FakeOpenAI):
    """Patch the ``openai`` module's ``OpenAI`` attribute so the inner
    ``from openai import OpenAI`` picks up our fake. Returns the
    patcher context manager."""
    import openai

    return patch.object(openai, "OpenAI", fake.factory())


def test_default_judge_callable_sends_prompt_as_system_payload_as_user(
    tmp_path: Path,
) -> None:
    """The callable's request shape is the judge contract: rubric in
    system, structured payload in user. A regression that swaps the
    roles would change the model's effective task."""
    fake = _FakeOpenAI()
    cfg = JudgeConfig(slot="A", model="judge-a-model", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg,
        env={"TRAJECTA_JUDGE_A_API_KEY": "k", "TRAJECTA_JUDGE_A_BASE_URL": "https://x"},
    )
    payload = {"run_id": "run_x", "extra": {"nested": [1, 2, 3]}}
    with _patch_openai(fake):
        raw = callable_("RUBRIC TEXT", payload)
    assert raw == '{"verdict": "acceptable", "rationale": "", "assertions": []}'
    # The fake recorded one provider call.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["model"] == "judge-a-model"
    # temperature=0 stamped for determinism — A4.3's κ rollup would be
    # noisy without it.
    assert call["temperature"] == 0
    messages = call["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "RUBRIC TEXT"
    assert messages[1]["role"] == "user"
    assert "run_x" in messages[1]["content"]
    assert "```json" in messages[1]["content"]


def test_default_judge_callable_threads_base_url_into_client(tmp_path: Path) -> None:
    """For Gemini-compatible endpoints the operator-supplied base URL
    must reach the OpenAI client constructor. Without this the call
    would land on api.openai.com regardless of slot A's env config."""
    fake = _FakeOpenAI()
    cfg = JudgeConfig(slot="A", model="judge-a-model", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg,
        env={
            "TRAJECTA_JUDGE_A_API_KEY": "gemini-key",
            "TRAJECTA_JUDGE_A_BASE_URL": "https://gemini.example/v1",
        },
    )
    with _patch_openai(fake):
        callable_("rubric", {"run_id": "r"})
    assert fake.construction_calls == [
        {"api_key": "gemini-key", "base_url": "https://gemini.example/v1"}
    ]


def test_default_judge_callable_omits_base_url_when_unset(tmp_path: Path) -> None:
    """Slot B without a base URL falls back to the SDK default. Passing
    ``base_url=None`` would override the SDK's own default, so the
    factory must omit the kwarg entirely."""
    fake = _FakeOpenAI()
    cfg = JudgeConfig(slot="B", model="openai-model", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg, env={"TRAJECTA_JUDGE_B_API_KEY": "openai-key"}
    )
    with _patch_openai(fake):
        callable_("rubric", {"run_id": "r"})
    assert fake.construction_calls == [{"api_key": "openai-key"}]


def test_default_judge_callable_uses_slot_A_model_not_slot_B(tmp_path: Path) -> None:
    """Cross-slot regression guard: slot A's call must carry slot A's
    model, even when slot B's env vars also happen to be set in the
    process. The factory closes over ``config.model`` at construction."""
    fake = _FakeOpenAI()
    cfg = JudgeConfig(slot="A", model="model-A", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg,
        env={
            "TRAJECTA_JUDGE_A_API_KEY": "ka",
            "TRAJECTA_JUDGE_B_API_KEY": "kb",
            "TRAJECTA_JUDGE_B_MODEL": "model-B",  # noise — must be ignored
        },
    )
    with _patch_openai(fake):
        callable_("rubric", {"run_id": "r"})
    assert fake.calls[0]["model"] == "model-A"


def test_default_judge_callable_uses_slot_B_model_not_slot_A(tmp_path: Path) -> None:
    fake = _FakeOpenAI()
    cfg = JudgeConfig(slot="B", model="model-B", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg,
        env={
            "TRAJECTA_JUDGE_B_API_KEY": "kb",
            "TRAJECTA_JUDGE_A_API_KEY": "ka",
            "TRAJECTA_JUDGE_A_MODEL": "model-A",  # noise — must be ignored
        },
    )
    with _patch_openai(fake):
        callable_("rubric", {"run_id": "r"})
    assert fake.calls[0]["model"] == "model-B"


def test_default_judge_callable_returns_provider_content_verbatim(
    tmp_path: Path,
) -> None:
    """The callable returns raw text; ``_parse_judge_response`` (one
    layer up) is responsible for verdict / assertion parsing. The
    raw passthrough keeps the parser tolerant of provider-specific
    response wrapping (code fences, etc.)."""
    raw_json = '```json\n{"verdict": "unacceptable", "rationale": "x", "assertions": []}\n```'
    fake = _FakeOpenAI(content=raw_json)
    cfg = JudgeConfig(slot="B", model="m", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg, env={"TRAJECTA_JUDGE_B_API_KEY": "k"}
    )
    with _patch_openai(fake):
        assert callable_("rubric", {"run_id": "r"}) == raw_json


def test_default_judge_callable_propagates_provider_exception_as_judge_error() -> None:
    """A 4xx from the provider, network timeout, etc. must surface as
    ``JudgeProviderError`` so the CLI / post-step can categorize it as
    a slot-level failure rather than letting the raw exception escape
    through the eval pipeline."""
    fake = _FakeOpenAI(exc=RuntimeError("simulated 401"))
    cfg = JudgeConfig(slot="A", model="m", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg, env={"TRAJECTA_JUDGE_A_API_KEY": "k"}
    )
    with _patch_openai(fake):
        with pytest.raises(JudgeProviderError, match="provider call failed"):
            callable_("rubric", {"run_id": "r"})


def test_default_judge_callable_rejects_empty_response_content() -> None:
    """An empty / non-string response would slip through the parser
    layer as a json.JSONDecodeError that doesn't name the slot. Catch
    it here so the operator sees the slot tag in the error."""
    fake = _FakeOpenAI(content="")
    cfg = JudgeConfig(slot="A", model="m", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg, env={"TRAJECTA_JUDGE_A_API_KEY": "k"}
    )
    with _patch_openai(fake):
        with pytest.raises(JudgeProviderError, match="empty or non-string"):
            callable_("rubric", {"run_id": "r"})


def test_default_judge_callable_rejects_non_string_response_content() -> None:
    """Some providers return structured tool-call outputs in
    ``message.content`` (e.g. a list of content blocks). The
    chat-completion contract says it should be a string — anything
    else is provider drift and should surface as a slot error."""
    fake = _FakeOpenAI(content=[{"type": "text", "text": "x"}])
    cfg = JudgeConfig(slot="A", model="m", prompt_version="v_acc")
    callable_ = _default_judge_callable(
        cfg, env={"TRAJECTA_JUDGE_A_API_KEY": "k"}
    )
    with _patch_openai(fake):
        with pytest.raises(JudgeProviderError, match="empty or non-string"):
            callable_("rubric", {"run_id": "r"})


def test_run_llm_judge_uses_default_callable_when_env_set(tmp_path: Path) -> None:
    """End-to-end: ``run_llm_judge`` without a ``judge_callable`` kwarg
    falls through to the env-driven default. The mocked OpenAI client
    completes the loop without a real network call."""
    fake = _FakeOpenAI(
        content=json.dumps(
            {
                "verdict": "acceptable",
                "rationale": "ok",
                "assertions": [
                    {"name": "verdict_alignment", "status": "pass", "rationale": "ok"}
                ],
            }
        )
    )
    cfg = JudgeConfig(slot="B", model="openai-test", prompt_version="v_acc")
    payload = {
        "run_id": "run_x",
        "golden_reference": {},
        "proposed_eval_case": {},
        "evidence_with_sources": [],
    }
    with patch.dict(
        "os.environ",
        {"TRAJECTA_JUDGE_B_API_KEY": "k"},
        clear=False,
    ):
        with _patch_openai(fake):
            result = run_llm_judge(
                payload, cfg, prompts_root=_judge_prompt_dir(tmp_path)
            )
    assert isinstance(result, JudgeLLMResult)
    assert result.verdict == "acceptable"
    assert result.slot == "B"
    assert result.model == "openai-test"
    # The mocked client was called exactly once.
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# A4.2 — provider-specific judge prompt bundles
#
# Both bundles must exist, list the six required assertion names
# verbatim, demand JSON-only output, and produce distinct sha256
# stamps. These tests lock in the contract that A4.3's κ_LLM,LLM
# rollup relies on: any silent edit that breaks rubric alignment fails
# CI before the κ number is computed.


_A42_REQUIRED_ASSERTION_NAMES = (
    "verdict_alignment",
    "failure_mode_compatibility",
    "failure_step_localization",
    "regression_case_usefulness",
    "no_forbidden_claim",
    "evidence_support",
)
_A42_PROVIDER_BUNDLES = ("v1_acceptability_gemini", "v1_acceptability_openai")


@pytest.mark.parametrize("version", _A42_PROVIDER_BUNDLES)
def test_prompt_a42_provider_specific_bundle_loads(version: str) -> None:
    """``load_judge_prompt`` resolves the new bundle and returns a
    non-empty text + 64-char sha256. A missing file would skip the
    judge silently in production via FileNotFoundError; we lock
    existence here."""
    text, sha = load_judge_prompt(version)
    assert text.strip(), f"{version} prompt.md is empty"
    assert len(sha) == 64


@pytest.mark.parametrize("version", _A42_PROVIDER_BUNDLES)
def test_prompt_a42_lists_all_six_assertion_names(version: str) -> None:
    """Both bundles must name every required assertion verbatim. A
    drift here turns A4.3's κ into a comparison of two different
    rubrics rather than two providers grading the same rubric."""
    text, _ = load_judge_prompt(version)
    missing = [name for name in _A42_REQUIRED_ASSERTION_NAMES if name not in text]
    assert not missing, (
        f"{version} prompt is missing required assertion names: {missing}"
    )


@pytest.mark.parametrize("version", _A42_PROVIDER_BUNDLES)
def test_prompt_a42_demands_json_only_output(version: str) -> None:
    """The judge parser only accepts a single JSON object (with a
    tolerant ```` ```json ``` ```` strip). Both bundles must explicitly
    tell the model to emit JSON-only — a bundle that lets the model
    free-form narrate would surface as a flood of parse errors at
    run time."""
    text, _ = load_judge_prompt(version)
    lowered = text.lower()
    # Lock in vocabulary: at least one phrase that signals "only JSON".
    # Tolerant to either bundle's exact phrasing so future provider
    # tweaks don't have to touch this test.
    phrases = (
        "only json",
        "return only",
        "raw json",
        "no preamble",
        "single json",
    )
    assert any(p in lowered for p in phrases), (
        f"{version} prompt does not request JSON-only output "
        f"(expected one of: {phrases})"
    )


@pytest.mark.parametrize("version", _A42_PROVIDER_BUNDLES)
def test_prompt_a42_declares_shared_verdict_vocabulary(version: str) -> None:
    """Both bundles must reference ``acceptable`` / ``unacceptable``
    verdicts and ``pass`` / ``fail`` status values so the parser's
    enum check (``_parse_judge_response``) matches the prompt's
    self-description."""
    text, _ = load_judge_prompt(version)
    lowered = text.lower()
    assert "acceptable" in lowered
    assert "unacceptable" in lowered
    assert "pass" in lowered
    assert "fail" in lowered


def test_prompt_a42_provider_bundles_have_distinct_sha256() -> None:
    """The provider-specific bundles are allowed to share rubric
    semantics, but they must not be byte-identical — A4.3's κ_LLM,LLM
    is a dual-provider check, and copy-pasted prompts would silently
    turn it into a single-prompt ablation."""
    _, sha_gemini = load_judge_prompt("v1_acceptability_gemini")
    _, sha_openai = load_judge_prompt("v1_acceptability_openai")
    assert sha_gemini != sha_openai


def test_prompt_a42_shared_baseline_still_loads() -> None:
    """The baseline ``v1_acceptability`` bundle is the rubric the two
    provider-specific variants follow. A4.2 must not delete it — a
    future operator may point both slots at it for a shared-prompt
    ablation, and the standalone CLI uses it as a documented default
    elsewhere in the suite."""
    text, sha = load_judge_prompt("v1_acceptability")
    assert "acceptable_eval_case" in text
    assert len(sha) == 64


# ---------------------------------------------------------------------------
# A4.3 — κ_LLM,LLM dual-judge agreement rollup
#
# Tests cover the builder (paired structure + κ math) and the writer
# (JSON shape + Markdown layout, with and without the κ < 0.6
# disagreement-analysis fallback). All deterministic; no real LLM
# anywhere — the helpers below assemble JudgeReports from compact
# verdict tuples to keep the agreement tests focused on rollup
# behaviour rather than fixture plumbing already exercised by A3.3.


_AGREEMENT_SHA_A = "a" * 64
_AGREEMENT_SHA_B = "b" * 64


def _judge_report(
    *,
    slot: str = "A",
    model: str = "gemini-mock",
    prompt_version: str = "v1_acceptability_gemini",
    prompt_sha256: str = _AGREEMENT_SHA_A,
    cases: list[tuple[str, str, list[JudgeAssertion] | None]] | None = None,
) -> JudgeReport:
    cases = cases or [("run_1", "acceptable", None)]
    judge = JudgeConfig(slot=slot, model=model, prompt_version=prompt_version)  # type: ignore[arg-type]
    case_reports = []
    for run_id, verdict, assertions in cases:
        case_reports.append(
            JudgeCaseReport(
                run_id=run_id,
                verdict=verdict,  # type: ignore[arg-type]
                rationale=f"{slot} says {verdict}",
                assertions=assertions or [],
            )
        )
    return JudgeReport(
        judge=judge,
        prompt_sha256=prompt_sha256,
        cases=case_reports,
    )


def _verdicts(*verdicts: str) -> list[tuple[str, str, list[JudgeAssertion] | None]]:
    """Compact factory: index-numbered run_ids paired with verdicts."""
    return [(f"run_{i + 1}", v, None) for i, v in enumerate(verdicts)]


# ---- build_judge_agreement_report -----------------------------------------


def test_agreement_kappa_perfect_when_verdicts_identical() -> None:
    """Two judges with byte-identical verdict streams yield κ = 1.0 by
    the formula in docs/testing.md § Cohen's κ."""
    a = _judge_report(
        slot="A",
        cases=_verdicts("acceptable", "acceptable", "unacceptable", "acceptable"),
    )
    b = _judge_report(
        slot="B",
        model="openai-mock",
        prompt_version="v1_acceptability_openai",
        prompt_sha256=_AGREEMENT_SHA_B,
        cases=_verdicts("acceptable", "acceptable", "unacceptable", "acceptable"),
    )
    report = build_judge_agreement_report(a, b)
    assert report.kappa == 1.0
    assert report.agreement_count == 4
    assert report.disagreement_count == 0
    assert report.needs_disagreement_analysis is False


def test_agreement_kappa_matches_hand_computed_on_mixed_stream() -> None:
    """5 cases, A=[T,T,T,F,F], B=[T,T,F,F,T] — hand-computed κ from
    docs/testing.md § cohens_kappa pseudocode: (0.6 - 0.52) / (1 - 0.52)
    = 0.08 / 0.48. The agreement builder must reach the same number
    cohens_kappa returns directly so the report-level κ stays
    auditable."""
    a = _judge_report(
        slot="A",
        cases=_verdicts(
            "acceptable", "acceptable", "acceptable", "unacceptable", "unacceptable"
        ),
    )
    b = _judge_report(
        slot="B",
        prompt_sha256=_AGREEMENT_SHA_B,
        cases=_verdicts(
            "acceptable", "acceptable", "unacceptable", "unacceptable", "acceptable"
        ),
    )
    report = build_judge_agreement_report(a, b)
    assert math.isclose(report.kappa, 0.08 / 0.48, abs_tol=1e-9)
    assert report.agreement_count == 3
    assert report.disagreement_count == 2


def test_agreement_rejects_swapped_slot_arguments() -> None:
    """build expects ``judge_a`` to be slot A and ``judge_b`` slot B —
    swapping them silently would mis-label every downstream column."""
    a = _judge_report(slot="A", cases=_verdicts("acceptable"))
    b = _judge_report(
        slot="B", prompt_sha256=_AGREEMENT_SHA_B, cases=_verdicts("acceptable")
    )
    # judge_a position with a slot-B report.
    with pytest.raises(ValueError, match="judge_a.slot='A'"):
        build_judge_agreement_report(b, b)
    with pytest.raises(ValueError, match="judge_b.slot='B'"):
        build_judge_agreement_report(a, a)


def test_agreement_rejects_mismatched_run_id_set() -> None:
    """A judge B graded on a different golden subset is a wiring bug —
    κ over disjoint cases is meaningless."""
    a = _judge_report(slot="A", cases=_verdicts("acceptable", "acceptable"))
    b = _judge_report(
        slot="B",
        prompt_sha256=_AGREEMENT_SHA_B,
        cases=[("run_1", "acceptable", None), ("run_X", "acceptable", None)],
    )
    with pytest.raises(ValueError, match="only_in_A|only_in_B"):
        build_judge_agreement_report(a, b)


def test_agreement_rejects_mismatched_run_id_order() -> None:
    """Even when the sets match, the ordering must match too — paired
    indices are how κ matches verdicts."""
    a = _judge_report(slot="A", cases=_verdicts("acceptable", "unacceptable"))
    b = _judge_report(
        slot="B",
        prompt_sha256=_AGREEMENT_SHA_B,
        cases=[
            ("run_2", "unacceptable", None),
            ("run_1", "acceptable", None),
        ],
    )
    with pytest.raises(ValueError, match="different run_id ordering"):
        build_judge_agreement_report(a, b)


def test_agreement_preserves_per_judge_acceptable_rate() -> None:
    """The single-judge ``acceptable_rate`` (already exposed by
    JudgeReport) must round-trip through the paired report — A7's
    experiment-log table reads these numbers."""
    a = _judge_report(
        slot="A", cases=_verdicts("acceptable", "acceptable", "unacceptable")
    )
    b = _judge_report(
        slot="B",
        prompt_sha256=_AGREEMENT_SHA_B,
        cases=_verdicts("acceptable", "unacceptable", "unacceptable"),
    )
    report = build_judge_agreement_report(a, b)
    assert report.judge_a.acceptable_rate == 2 / 3
    assert report.judge_b.acceptable_rate == 1 / 3


def test_agreement_default_selection_policy() -> None:
    a = _judge_report(slot="A", cases=_verdicts("acceptable"))
    b = _judge_report(
        slot="B", prompt_sha256=_AGREEMENT_SHA_B, cases=_verdicts("acceptable")
    )
    report = build_judge_agreement_report(a, b)
    assert report.selection_policy == DEFAULT_SELECTION_POLICY


def test_agreement_custom_selection_policy_threaded_through() -> None:
    """A cost-constrained run must be able to stamp its own policy
    string. docs/testing.md § Sample policy doesn't prescribe the exact
    label, so the field stays opaque — but it must reach the report."""
    a = _judge_report(slot="A", cases=_verdicts("acceptable"))
    b = _judge_report(
        slot="B", prompt_sha256=_AGREEMENT_SHA_B, cases=_verdicts("acceptable")
    )
    report = build_judge_agreement_report(
        a, b, selection_policy="deterministic_stratified_n10"
    )
    assert report.selection_policy == "deterministic_stratified_n10"


# ---- write_judge_agreement_report — JSON shape ----------------------------


def _two_judges(
    *,
    a_verdicts: list[str],
    b_verdicts: list[str],
    a_assertions: list[list[JudgeAssertion] | None] | None = None,
    b_assertions: list[list[JudgeAssertion] | None] | None = None,
) -> tuple[JudgeReport, JudgeReport]:
    """Helper: build two slot-paired reports from parallel verdict
    lists with optional per-case assertion overrides."""

    def _cases(
        verdicts: list[str],
        assertions: list[list[JudgeAssertion] | None] | None,
    ) -> list[tuple[str, str, list[JudgeAssertion] | None]]:
        out: list[tuple[str, str, list[JudgeAssertion] | None]] = []
        for i, verdict in enumerate(verdicts):
            extra = assertions[i] if assertions and i < len(assertions) else None
            out.append((f"run_{i + 1}", verdict, extra))
        return out

    a = _judge_report(
        slot="A",
        model="gemini-mock",
        prompt_version="v1_acceptability_gemini",
        prompt_sha256=_AGREEMENT_SHA_A,
        cases=_cases(a_verdicts, a_assertions),
    )
    b = _judge_report(
        slot="B",
        model="openai-mock",
        prompt_version="v1_acceptability_openai",
        prompt_sha256=_AGREEMENT_SHA_B,
        cases=_cases(b_verdicts, b_assertions),
    )
    return a, b


def test_write_agreement_json_carries_kappa_sample_size_policy_and_judges(
    tmp_path: Path,
) -> None:
    """JSON top-level keys are the contract the experiment log + the
    Acceptance Checklist read from. This test locks in the documented
    shape rather than the cosmetic ordering."""
    a, b = _two_judges(
        a_verdicts=["acceptable", "acceptable", "unacceptable"],
        b_verdicts=["acceptable", "unacceptable", "unacceptable"],
    )
    report = build_judge_agreement_report(
        a, b, selection_policy="deterministic_stratified_n3"
    )
    json_path, _ = write_judge_agreement_report(
        report, tmp_path / "judge_report.json"
    )
    data = json.loads(json_path.read_text(encoding="utf-8"))

    assert data["sample_size"] == 3
    assert data["selection_policy"] == "deterministic_stratified_n3"
    assert "kappa_llm_llm" in data
    assert data["kappa_threshold"] == 0.6
    assert data["judges"]["A"]["slot"] == "A"
    assert data["judges"]["A"]["model"] == "gemini-mock"
    assert data["judges"]["A"]["prompt_version"] == "v1_acceptability_gemini"
    assert data["judges"]["A"]["prompt_sha256"] == _AGREEMENT_SHA_A
    assert data["judges"]["B"]["slot"] == "B"
    assert data["judges"]["B"]["model"] == "openai-mock"
    assert data["judges"]["B"]["prompt_sha256"] == _AGREEMENT_SHA_B
    assert data["judges"]["A"]["acceptable_rate"] == 2 / 3
    assert data["judges"]["B"]["acceptable_rate"] == 1 / 3
    assert [c["run_id"] for c in data["cases"]] == ["run_1", "run_2", "run_3"]
    assert data["cases"][0]["agree"] is True
    assert data["cases"][1]["agree"] is False
    assert data["agreement_count"] == 2
    assert data["disagreement_count"] == 1


def test_write_agreement_json_disagreements_list_failed_assertions(
    tmp_path: Path,
) -> None:
    """For each disagreement case the JSON surfaces the failed
    assertion names per side. A4 disagreement-analysis fallback uses
    this list to categorise the split."""
    failed_a = [
        JudgeAssertion(
            name="verdict_alignment", status="pass", rationale="ok"
        ),
        JudgeAssertion(
            name="evidence_support", status="fail", rationale="missing"
        ),
    ]
    failed_b = [
        JudgeAssertion(
            name="verdict_alignment", status="fail", rationale="mismatch"
        )
    ]
    a, b = _two_judges(
        a_verdicts=["acceptable", "unacceptable"],
        b_verdicts=["acceptable", "acceptable"],
        a_assertions=[None, failed_a],
        b_assertions=[None, failed_b],
    )
    report = build_judge_agreement_report(a, b)
    json_path, _ = write_judge_agreement_report(
        report, tmp_path / "judge_report.json"
    )
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert len(data["disagreements"]) == 1
    row = data["disagreements"][0]
    assert row["run_id"] == "run_2"
    assert row["judge_a"]["failed_assertions"] == ["evidence_support"]
    assert row["judge_b"]["failed_assertions"] == ["verdict_alignment"]


# ---- write_judge_agreement_report — Markdown shape ------------------------


def test_write_agreement_md_contains_kappa_row_and_per_judge_rates(
    tmp_path: Path,
) -> None:
    """Markdown headline numbers are what an operator reads in 10
    seconds. Keep the strings stable so the doc snapshot in the
    experiment log can quote them verbatim."""
    a, b = _two_judges(
        a_verdicts=["acceptable", "acceptable", "unacceptable"],
        b_verdicts=["acceptable", "unacceptable", "unacceptable"],
    )
    report = build_judge_agreement_report(a, b)
    _, md_path = write_judge_agreement_report(
        report, tmp_path / "judge_report.json"
    )
    md = md_path.read_text(encoding="utf-8")

    assert md.startswith("# Judge Agreement Report")
    assert "Judge A (slot A)" in md
    assert "`gemini-mock`" in md
    assert "`v1_acceptability_gemini`" in md
    assert "Judge B (slot B)" in md
    assert "`openai-mock`" in md
    assert "`v1_acceptability_openai`" in md
    assert "| A | 2 / 3 | 66.7% |" in md
    assert "| B | 1 / 3 | 33.3% |" in md
    assert "κ_LLM,LLM" in md
    assert "target ≥ 0.6" in md
    assert "Selection policy: `full_31_preferred`" in md


def test_write_agreement_md_renders_disagreement_section_when_kappa_below_threshold(
    tmp_path: Path,
) -> None:
    """5 cases with three splits → κ ≈ -0.05 (well below 0.6). The
    Markdown writer must render the Disagreement Analysis section and
    list every disagreeing case."""
    a, b = _two_judges(
        a_verdicts=[
            "acceptable",
            "acceptable",
            "unacceptable",
            "acceptable",
            "unacceptable",
        ],
        b_verdicts=[
            "unacceptable",
            "acceptable",
            "acceptable",
            "unacceptable",
            "unacceptable",
        ],
    )
    report = build_judge_agreement_report(a, b)
    assert report.kappa < 0.6
    assert report.needs_disagreement_analysis is True

    _, md_path = write_judge_agreement_report(
        report, tmp_path / "judge_report.json"
    )
    md = md_path.read_text(encoding="utf-8")
    assert "## Disagreement Analysis" in md
    assert "BELOW THRESHOLD" in md
    assert "### `run_1`" in md
    assert "### `run_3`" in md
    assert "### `run_4`" in md
    assert "### `run_2`" not in md
    assert "### `run_5`" not in md


def test_write_agreement_md_omits_disagreement_section_when_kappa_above_threshold(
    tmp_path: Path,
) -> None:
    """κ ≥ 0.6 ⇒ no Disagreement Analysis section. The Markdown should
    still mention the per-case table and the kappa row, but skipping
    the long-form analysis keeps the report digestible when the run
    actually agreed."""
    a, b = _two_judges(
        a_verdicts=["acceptable", "acceptable", "unacceptable", "unacceptable"],
        b_verdicts=["acceptable", "acceptable", "unacceptable", "unacceptable"],
    )
    report = build_judge_agreement_report(a, b)
    assert report.needs_disagreement_analysis is False
    _, md_path = write_judge_agreement_report(
        report, tmp_path / "judge_report.json"
    )
    md = md_path.read_text(encoding="utf-8")
    assert "## Disagreement Analysis" not in md
    assert "PASS" in md
    assert "## Per-case verdicts" in md


def test_write_agreement_md_disagreement_section_lists_failed_assertions(
    tmp_path: Path,
) -> None:
    """Each disagreement subsection must list both judges' failed
    assertion names so the operator can see *why* the split happened
    without paging through the full per-case JSON."""
    failed_a = [
        JudgeAssertion(name="verdict_alignment", status="pass", rationale="ok"),
        JudgeAssertion(name="evidence_support", status="fail", rationale="missing"),
    ]
    failed_b = [
        JudgeAssertion(
            name="failure_mode_compatibility", status="fail", rationale="wrong type"
        )
    ]
    a, b = _two_judges(
        a_verdicts=["acceptable", "unacceptable"],
        b_verdicts=["unacceptable", "acceptable"],
        a_assertions=[None, failed_a],
        b_assertions=[None, failed_b],
    )
    report = build_judge_agreement_report(a, b)
    assert report.needs_disagreement_analysis is True
    _, md_path = write_judge_agreement_report(
        report, tmp_path / "judge_report.json"
    )
    md = md_path.read_text(encoding="utf-8")
    assert "`evidence_support`" in md
    assert "`failure_mode_compatibility`" in md


def test_write_agreement_creates_parent_directory(tmp_path: Path) -> None:
    """The writer must auto-create its parent dir — the dual-judge
    artefact lands under ``<archive>/judge/agreement/`` (or similar)
    which may not exist yet on first run."""
    a, b = _two_judges(
        a_verdicts=["acceptable"], b_verdicts=["acceptable"]
    )
    report = build_judge_agreement_report(a, b)
    out = tmp_path / "nested" / "deep" / "judge_report.json"
    json_path, md_path = write_judge_agreement_report(report, out)
    assert json_path.exists()
    assert md_path.exists()


def test_agreement_case_agree_property_matches_paired_verdicts() -> None:
    """Sanity: the JSON ``agree`` field is the per-case property, not a
    separately-tracked counter. Drift between the two would corrupt
    agreement_count downstream."""
    ca = JudgeCaseReport(
        run_id="run_1", verdict="acceptable", rationale="x", assertions=[]
    )
    cb_same = JudgeCaseReport(
        run_id="run_1", verdict="acceptable", rationale="y", assertions=[]
    )
    cb_diff = JudgeCaseReport(
        run_id="run_1", verdict="unacceptable", rationale="z", assertions=[]
    )
    assert JudgeAgreementCase(run_id="run_1", judge_a=ca, judge_b=cb_same).agree is True
    assert JudgeAgreementCase(run_id="run_1", judge_a=ca, judge_b=cb_diff).agree is False
