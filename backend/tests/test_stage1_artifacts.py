from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.app.schemas import AgentTrace, EvalCase, FailureMemoryCase, TrajectoryRun


REPO_ROOT = Path(__file__).resolve().parents[2]


class Stage1ArtifactTests(unittest.TestCase):
    def load_runs(self) -> list[tuple[Path, TrajectoryRun]]:
        runs = []
        for path in sorted((REPO_ROOT / "data/runs").glob("*/trajectory.json")):
            runs.append((path, TrajectoryRun.model_validate_json(path.read_text(encoding="utf-8"))))
        return runs

    def test_fixture_runs_validate_and_have_screenshots(self) -> None:
        runs = self.load_runs()
        self.assertGreaterEqual(len(runs), 5)

        for path, run in runs:
            screenshot_dir = path.parent / "screenshots"
            self.assertTrue(run.steps, run.run_id)
            for step in run.steps:
                screenshot = step.observation.screenshot
                if screenshot is not None:
                    self.assertTrue((screenshot_dir / screenshot).exists(), f"{run.run_id}/{screenshot}")

    def test_status_overlay_covers_fixtures_with_success_per_category(self) -> None:
        overlay_path = REPO_ROOT / "data/raw/molmoweb_humanskills_sample/run_status_overlay.json"
        overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
        runs = self.load_runs()

        for _, run in runs:
            self.assertIn(run.run_id, overlay)
            self.assertEqual(run.status, overlay[run.run_id])

        categories = {run.metadata.get("category") for _, run in runs if run.metadata.get("category")}
        for category in categories:
            self.assertTrue(
                any(run.metadata.get("category") == category and run.status == "success" for _, run in runs),
                f"missing success fixture for category {category}",
            )

    def test_failure_memory_seed_validates(self) -> None:
        cases_path = REPO_ROOT / "data/failure_memory/cases.jsonl"
        cases = [
            FailureMemoryCase.model_validate_json(line)
            for line in cases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        case_ids = [case.case_id for case in cases]

        self.assertGreaterEqual(len(cases), 5)
        self.assertEqual(len(case_ids), len(set(case_ids)))
        self.assertIn("missed_constraint", {case.failure_type for case in cases})

    def test_validated_eval_case_fixture_validates(self) -> None:
        case_paths = sorted((REPO_ROOT / "data/eval_cases/validated").glob("*.json"))
        self.assertTrue(case_paths)
        run_ids = {run.run_id for _, run in self.load_runs()}

        for path in case_paths:
            case = EvalCase.model_validate_json(path.read_text(encoding="utf-8"))
            self.assertTrue(case.human_validated)
            self.assertIn(case.source_run_id, run_ids)
            self.assertTrue(case.evidence)

    def test_example_trace_validates_and_is_monotonic(self) -> None:
        trace_paths = sorted((REPO_ROOT / "data/runs").glob("*/last_trace.json"))
        self.assertTrue(trace_paths)

        for path in trace_paths:
            trace = AgentTrace.model_validate_json(path.read_text(encoding="utf-8"))
            seqs = [event.seq for event in trace.events]
            turns = [event.turn for event in trace.events]
            self.assertEqual(seqs, list(range(len(seqs))))
            self.assertEqual(turns, sorted(turns))
            if trace.terminated_by == "propose_eval_case":
                self.assertTrue(
                    any(event.type == "tool_call" and event.name == "propose_eval_case" for event in trace.events)
                )


if __name__ == "__main__":
    unittest.main()
