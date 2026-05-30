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
    JudgeAssertion,
    JudgeCaseReport,
    JudgeConfig,
    JudgeLLMResult,
    JudgeReport,
    StandaloneJudgeResult,
    aggregate_verdict,
    build_arg_parser,
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
    write_judge_report,
)
from eval.judge import _parse_judge_response  # noqa: E402 — direct test access

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


def test_run_llm_judge_default_callable_raises_without_provider(tmp_path: Path) -> None:
    """No real provider is wired in A3.2 — a caller that forgets to pass
    a callable gets a clear NotImplementedError instead of an accidental
    network call."""
    config = JudgeConfig(slot="A", model="gemini-flash-test", prompt_version="v_acc")
    payload = {"run_id": "r", "golden_reference": {}, "proposed_eval_case": {}, "evidence_with_sources": []}
    with pytest.raises(NotImplementedError, match="judge_callable"):
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


def test_cli_main_default_callable_returns_not_implemented_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Until A4.1 wires real provider clients, ``main`` without a
    callable resolves to ``_default_judge_callable`` which raises
    ``NotImplementedError``. The CLI converts that to exit code 3 with
    a hint pointing at A4.1 — not a stack trace dumped to the operator."""
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
    assert "A4.1" in stderr
