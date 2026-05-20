from __future__ import annotations

import json
from pathlib import Path

from .schemas import EvalCase, FailureMemoryCase, TrajectoryRun


class LocalStorage:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(__file__).resolve().parents[2] / "data"

    def list_runs(self) -> list[str]:
        runs_dir = self.root / "runs"
        if not runs_dir.exists():
            return []
        return sorted([p.name for p in runs_dir.iterdir() if p.is_dir()])

    def load_run(self, run_id: str) -> TrajectoryRun:
        run_file = self.root / "runs" / run_id / "trajectory.json"
        with run_file.open("r", encoding="utf-8") as f:
            return TrajectoryRun.model_validate(json.load(f))

    def load_failure_memory(self) -> list[FailureMemoryCase]:
        memory_file = self.root / "failure_memory" / "cases.jsonl"
        if not memory_file.exists():
            return []
        cases: list[FailureMemoryCase] = []
        with memory_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                cases.append(FailureMemoryCase.model_validate_json(line))
        return cases

    def save_eval_case(self, eval_case: EvalCase) -> Path:
        out_dir = self.root / "eval_cases" / "generated"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{eval_case.eval_case_id}.json"
        out_path.write_text(eval_case.model_dump_json(indent=2), encoding="utf-8")
        return out_path
