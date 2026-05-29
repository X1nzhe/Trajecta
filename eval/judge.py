"""LLM-judge for the Trajecta Eval Agent.

This module implements S18 § 2.2 Build 4: an LLM judge that scores one
quality dimension — ``acceptable_eval_case`` — over the proposed
``EvalCase`` for each golden-set case, and reports Cohen's κ against a
second annotator (another LLM or a human-labelled subset).

The rubric is the six-clause scheme defined in ``docs/testing.md``
§ "LLM Judge":

    1. Verdict match                             (mechanical)
    2. Failure-type compatibility                (mechanical)
    3. Failure-step locality                     (mechanical)
    4. No contradiction with expected facts      (mechanical)
    5. No forbidden assertions                   (mechanical)
    6. Evidence traceability                     (LLM)

Five of six clauses are decided **deterministically** from the
structured ``expected_facts`` / ``forbidden_facts`` in
``eval/golden.jsonl`` against the proposed ``EvalCase`` fields. Only
clause 6 requires an LLM call. That split means:

  * Cohen's κ across two LLM judges varies only on clause 6 — the
    deterministic clauses produce identical answers for any annotator.
    The disagreement-analysis section therefore points squarely at the
    qualitative dimension, which is the failure mode worth surfacing.
  * A3.1 (this commit) ships the mechanical foundation, loaders, and
    Cohen's κ math — all offline-testable. A3.2 adds source resolution,
    the LLM clause-6 call, the CLI, and the report writers.

Run as ``python -m eval.judge`` once A3.2 lands.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the repo root importable so ``from backend.app.schemas import ...``
# works regardless of which directory the script was launched from.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app.schemas import (  # noqa: E402
    AgentTrace,
    FailureStepFact,
    FailureTypeFact,
    GoldenCase,
    OutcomeFact,
)

# Default artefact locations match the convention established by A1
# (eval/golden.jsonl committed) and A2 (eval/runs/<stamp>/traces/
# generated locally). The CLI in A3.2 will accept overrides.
DEFAULT_GOLDEN_PATH = _REPO_ROOT / "eval" / "golden.jsonl"

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
    """Result of running the six rubric clauses against one proposed case.

    Each clause carries one of three values:

      * ``True``  — the clause holds.
      * ``False`` — the clause was checked and failed.
      * ``None``  — the clause does not apply to this golden reference
                    (e.g. clause 2 on a success-shape reference). N/A
                    does **not** count as a failure when aggregating
                    the verdict.

    Clause 6 is left as ``None`` by A3.1 (it requires the LLM); A3.2
    populates it.
    """

    clause_1_verdict_match: bool | None
    clause_2_failure_type: bool | None
    clause_3_failure_step: bool | None
    clause_4_expected_facts: bool | None
    clause_5_no_forbidden: bool | None
    clause_6_evidence_grounded: bool | None

    def as_dict(self) -> dict[int, bool | None]:
        return {
            1: self.clause_1_verdict_match,
            2: self.clause_2_failure_type,
            3: self.clause_3_failure_step,
            4: self.clause_4_expected_facts,
            5: self.clause_5_no_forbidden,
            6: self.clause_6_evidence_grounded,
        }


# ---------------------------------------------------------------------------
# Loaders


def load_golden_cases(path: Path = DEFAULT_GOLDEN_PATH) -> dict[str, GoldenCase]:
    """Load ``eval/golden.jsonl`` into a ``run_id → GoldenCase`` mapping.

    Each row is validated through ``GoldenCase.model_validate``; a stale
    JSONL with the old free-text fact shape raises during validation
    rather than producing silently-wrong judgments. Duplicate run_ids
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
            if case.input.run_id in cases:
                raise ValueError(
                    f"duplicate run_id {case.input.run_id!r} in {path}; "
                    f"each golden row must be unique"
                )
            cases[case.input.run_id] = case
    return cases


def load_trace(trace_dir: Path, run_id: str) -> AgentTrace:
    """Load a per-sample trace dump produced by ``agent_eval.py --trace-dir``.

    Phase 8 A2 introduced this on-disk format: one ``{run_id}.json`` per
    gradeable sample under ``eval/runs/<stamp>/traces/``. The file
    contents are ``AgentTrace.model_dump_json(indent=2)``.
    """
    path = trace_dir / f"{run_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"trace dump for run_id={run_id!r} not found at {path}; "
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
    failed, which 4 by itself cannot — the failed-rubrics list in
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
    """Run clauses 1-5 against one (golden, proposed) pair.

    Clause 6 (the LLM clause) is left ``None``; A3.2 fills it in.
    """
    return ClauseEvaluation(
        clause_1_verdict_match=clause_1_verdict_match(golden, proposed),
        clause_2_failure_type=clause_2_failure_type_compatibility(golden, proposed),
        clause_3_failure_step=clause_3_failure_step_locality(golden, proposed),
        clause_4_expected_facts=clause_4_expected_facts_satisfied(golden, proposed),
        clause_5_no_forbidden=clause_5_no_forbidden_assertions(golden, proposed),
        clause_6_evidence_grounded=None,
    )


# ---------------------------------------------------------------------------
# Verdict aggregation


def aggregate_verdict(
    clauses: ClauseEvaluation,
) -> tuple[str, list[int]]:
    """Collapse a ``ClauseEvaluation`` into a binary verdict and the list
    of failed clause numbers.

    Rules (per ``docs/testing.md`` § LLM Judge):

      * A case is ``acceptable`` iff every clause is ``True`` or ``None``.
      * ``None`` (clause does not apply) does **not** count as a failure.
      * ``failed_rubrics`` is the sorted list of clause numbers whose
        value is ``False``; empty when the verdict is ``acceptable``.

    Clause 6 left as ``None`` — the A3.1-only path — is treated as
    "not yet judged"; the verdict is "acceptable" iff the deterministic
    clauses all pass. A3.2 always populates clause 6, so a real judge
    run never silently skips it.
    """
    failed: list[int] = []
    for n, value in clauses.as_dict().items():
        if value is False:
            failed.append(n)
    verdict = "acceptable" if not failed else "not_acceptable"
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


__all__ = [
    "ClauseEvaluation",
    "aggregate_verdict",
    "clause_1_verdict_match",
    "clause_2_failure_type_compatibility",
    "clause_3_failure_step_locality",
    "clause_4_expected_facts_satisfied",
    "clause_5_no_forbidden_assertions",
    "cohens_kappa",
    "disagreement_indices",
    "evaluate_mechanical_clauses",
    "extract_proposed_eval_case",
    "load_golden_cases",
    "load_trace",
]
