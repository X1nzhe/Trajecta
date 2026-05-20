from __future__ import annotations

import argparse

from .rag import score_case_similarity
from .schemas import FailureMemoryCase
from .storage import LocalStorage


def run_lightweight_ragas_like_eval(run_id: str, step_id: str) -> dict[str, float]:
    storage = LocalStorage()
    run = storage.load_run(run_id)
    step = next(s for s in run.steps if s.step_id == step_id)
    cases = storage.load_failure_memory()

    if not cases:
        return {"context_precision": 0.0}

    query = f"{step.action} {step.error or ''}".strip()
    scores = [score_case_similarity(query, c) for c in cases]
    return {"context_precision": max(scores)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run lightweight RAGAS-like eval")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--step-id", required=True)
    args = parser.parse_args()

    result = run_lightweight_ragas_like_eval(args.run_id, args.step_id)
    print(result)


if __name__ == "__main__":
    main()
