from __future__ import annotations

from .schemas import EvalAnalysis, EvalCase, FailureMemoryCase, TrajectoryRun, TrajectoryStep


def generate_eval_case(
    run: TrajectoryRun,
    step: TrajectoryStep,
    analysis: EvalAnalysis,
    retrieved_cases: list[FailureMemoryCase],
) -> EvalCase:
    return EvalCase(
        eval_case_id=f"{run.run_id}_{step.step_id}_{analysis.failure_label}",
        run_id=run.run_id,
        step_id=step.step_id,
        failure_label=analysis.failure_label,
        status="draft",
        summary=(
            f"Run {run.run_id} step {step.step_id} likely failed with "
            f"'{analysis.failure_label}' (confidence {analysis.confidence:.2f})."
        ),
        evidence=[
            f"action={step.action}",
            f"target={step.target or 'n/a'}",
            f"reasoning={analysis.reasoning}",
        ],
        similar_case_ids=[c.case_id for c in retrieved_cases],
    )
