from __future__ import annotations

from .rag import retrieve_similar_cases
from .schemas import EvalAnalysis, FailureMemoryCase, TrajectoryRun, TrajectoryStep


def get_step(run: TrajectoryRun, step_id: str) -> TrajectoryStep:
    for step in run.steps:
        if step.step_id == step_id:
            return step
    raise KeyError(f"Step {step_id} not found")


def analyze_step(step: TrajectoryStep) -> EvalAnalysis:
    if not step.success or step.error:
        label = "action_failed"
        confidence = 0.85
        reason = step.error or "Step marked as unsuccessful"
    elif "timeout" in step.action.lower() or "wait" in step.action.lower():
        label = "timing_issue"
        confidence = 0.6
        reason = "Potential timing sensitivity detected in action text"
    else:
        label = "unknown"
        confidence = 0.35
        reason = "No clear failure signal found; requires human review"

    return EvalAnalysis(failure_label=label, confidence=confidence, reasoning=reason)


def retrieve_failure_memories(
    query: str, memory_cases: list[FailureMemoryCase], top_k: int = 3
) -> list[FailureMemoryCase]:
    return retrieve_similar_cases(query=query, cases=memory_cases, top_k=top_k)
