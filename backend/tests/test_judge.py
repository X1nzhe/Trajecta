"""Phase 8 A3.1 — tests for the LLM-judge mechanical foundation.

Covers the 5 deterministic rubric clauses, Cohen's κ math, and the
loaders that bridge ``eval/golden.jsonl`` and the per-sample trace
dumps produced by ``backend.app.agent_eval --trace-dir``.

Clause 6 (the LLM call) ships in A3.2 and is tested separately there.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from backend.app.schemas import (
    AgentTrace,
    AgentTraceEvent,
    FailureStepFact,
    FailureTypeFact,
    GoldenCase,
    OutcomeFact,
)
from eval.judge import (
    ClauseEvaluation,
    aggregate_verdict,
    clause_1_verdict_match,
    clause_2_failure_type_compatibility,
    clause_3_failure_step_locality,
    clause_4_expected_facts_satisfied,
    clause_5_no_forbidden_assertions,
    cohens_kappa,
    disagreement_indices,
    evaluate_mechanical_clauses,
    extract_proposed_eval_case,
    load_golden_cases,
    load_trace,
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
    the clause when computing failed_rubrics."""
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
    assert verdict == "not_acceptable"
    assert failed == [2, 4, 5]


def test_aggregate_verdict_ignores_none_clauses() -> None:
    """N/A clauses do not count as failures. A success reference with a
    matching success proposal should be acceptable even though clauses
    2 and 3 are None."""
    golden = _success_golden()
    proposed = _matched_success_proposal()
    clauses = evaluate_mechanical_clauses(golden, proposed)
    # Clauses 2 and 3 are N/A — they must not appear in failed_rubrics.
    assert clauses.clause_2_failure_type is None
    assert clauses.clause_3_failure_step is None
    verdict, failed = aggregate_verdict(clauses)
    assert verdict == "acceptable"
    assert failed == []


def test_aggregate_verdict_treats_clause_6_none_as_not_failure() -> None:
    """A3.1 leaves clause 6 as None (LLM call lives in A3.2). The
    aggregate must still produce a defensible verdict on the
    mechanical clauses alone — otherwise the A3.1 commit cannot be
    independently smoke-tested."""
    clauses = ClauseEvaluation(
        clause_1_verdict_match=True,
        clause_2_failure_type=True,
        clause_3_failure_step=True,
        clause_4_expected_facts=True,
        clause_5_no_forbidden=True,
        clause_6_evidence_grounded=None,
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
