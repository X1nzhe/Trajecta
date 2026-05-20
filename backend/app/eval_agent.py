from __future__ import annotations

from .eval_case_generator import generate_eval_case
from .storage import LocalStorage
from .tools import analyze_step, get_step, retrieve_failure_memories


class EvalAgent:
    def __init__(self, storage: LocalStorage | None = None) -> None:
        self.storage = storage or LocalStorage()

    def analyze_step(self, run_id: str, step_id: str):
        run = self.storage.load_run(run_id)
        step = get_step(run, step_id)
        analysis = analyze_step(step)
        memories = self.storage.load_failure_memory()
        retrieved = retrieve_failure_memories(
            query=f"{step.action} {step.error or ''}", memory_cases=memories
        )
        eval_case = generate_eval_case(run, step, analysis, retrieved)
        self.storage.save_eval_case(eval_case)
        return {
            "analysis": analysis.model_dump(),
            "eval_case": eval_case.model_dump(),
            "retrieved_cases": [c.model_dump() for c in retrieved],
        }
