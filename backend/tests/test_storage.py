from __future__ import annotations

import json
import unittest

from backend.app import storage
from backend.app.schemas import (
    AgentTrace,
    EvalCase,
    EvidenceItem,
    FailureMemoryCase,
    StepAction,
    StepObservation,
    TrajectoryDigest,
    Trajectory,
    TrajectoryStep,
)


def sample_run(trajectory_id: str = "run_1", status: str = "unknown") -> Trajectory:
    return Trajectory(
        trajectory_id=trajectory_id,
        task="Find a result",
        status=status,
        steps=[
            TrajectoryStep(
                index=0,
                observation=StepObservation(screenshot="screenshot_001.png"),
                action=StepAction(type="wait", raw="wait()"),
            )
        ],
    )


def sample_eval_case(case_id: str = "ec_run_1_step_0", source_trajectory_id: str = "run_1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        source_trajectory_id=source_trajectory_id,
        task="Find a result",
        failure_step=0,
        failure_type="early_terminated",
        expected_behavior="The agent should finish the task.",
        actual_behavior="The agent stopped before finishing.",
        evidence=[EvidenceItem(claim="Step 0 stopped.", source="trajectory", trajectory_id=source_trajectory_id, step_index=0)],
        regression_rule="Do not stop before task evidence is visible.",
        human_validated=True,
    )


def sample_success_eval_case(case_id: str = "ec_run_1_success", source_trajectory_id: str = "run_1") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        source_trajectory_id=source_trajectory_id,
        task="Find a result",
        evidence=[
            EvidenceItem(
                claim="Step 0 reached the expected page state.",
                source="trajectory",
                trajectory_id=source_trajectory_id,
                step_index=0,
            )
        ],
        human_validated=True,
    )


class StorageTests(unittest.TestCase):
    def test_save_and_load_trajectory(self) -> None:
        run = sample_run()

        storage.save_trajectory(run)
        loaded = storage.load_trajectory("run_1")

        self.assertEqual(loaded.trajectory_id, "run_1")
        self.assertEqual(loaded.steps[0].action.type, "wait")

    def test_save_trajectory_preserves_trace_and_screenshots(self) -> None:
        """Re-import must not cascade-delete the user's prior analysis.

        Documented in docs/dataset_import.md "Re-Import Behavior": traces
        survive re-import; screenshots are upserted (not orphan-deleted).
        Regression guard for the cascade-delete bug Copilot caught.
        """

        storage.save_trajectory(sample_run())
        storage.save_screenshots("run_1", {"screenshot_001.png": b"png-bytes"})
        trace = AgentTrace(trajectory_id="run_1", user_intent="analyze_trajectory")
        storage.save_trace("run_1", trace)
        digest = TrajectoryDigest(trajectory_id="run_1", task="Find a result", step_count=0, steps=[])
        storage.save_digest("run_1", digest)

        storage.save_trajectory(sample_run(status="failed"))

        self.assertIsNotNone(storage.load_trace("run_1"))
        self.assertEqual(storage.load_screenshot("run_1", "screenshot_001.png"), b"png-bytes")
        self.assertIsNotNone(storage.load_digest("run_1"))
        self.assertEqual(storage.load_trajectory("run_1").status, "failed")

    def test_save_trajectory_replaces_existing(self) -> None:
        original = sample_run()
        storage.save_trajectory(original)

        modified = Trajectory(
            trajectory_id="run_1",
            task="Updated task",
            status="failed",
            steps=[
                TrajectoryStep(
                    index=0,
                    observation=StepObservation(screenshot="x.png"),
                    action=StepAction(type="click", raw="click(0,0)"),
                ),
                TrajectoryStep(
                    index=1,
                    observation=StepObservation(),
                    action=StepAction(type="wait", raw="wait()"),
                ),
            ],
        )
        storage.save_trajectory(modified)

        loaded = storage.load_trajectory("run_1")
        self.assertEqual(loaded.task, "Updated task")
        self.assertEqual(loaded.status, "failed")
        self.assertEqual(len(loaded.steps), 2)

    def test_list_trajectories(self) -> None:
        storage.save_trajectory(sample_run("run_b"))
        storage.save_trajectory(sample_run("run_a"))

        self.assertEqual([run.trajectory_id for run in storage.list_trajectories()], ["run_a", "run_b"])

    def test_load_missing_digest_returns_none(self) -> None:
        self.assertIsNone(storage.load_digest("run_1"))

    def test_save_and_load_trace(self) -> None:
        storage.save_trajectory(sample_run())
        trace = AgentTrace(trajectory_id="run_1", user_intent="analyze_trajectory")

        storage.save_trace("run_1", trace)
        loaded = storage.load_trace("run_1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.trajectory_id, "run_1")

    def test_save_and_load_digest(self) -> None:
        storage.save_trajectory(sample_run())
        digest = TrajectoryDigest(trajectory_id="run_1", task="Find a result", step_count=0, steps=[])

        storage.save_digest("run_1", digest)
        loaded = storage.load_digest("run_1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task, "Find a result")

    def test_save_eval_case_refuses_overwrite(self) -> None:
        storage.save_trajectory(sample_run())
        case = sample_eval_case()

        storage.save_eval_case(case)

        with self.assertRaises(FileExistsError):
            storage.save_eval_case(case)

    def test_load_failure_memory_rejects_duplicate_case_id(self) -> None:
        cases_dir = storage.data_dir() / "failure_memory"
        cases_dir.mkdir(parents=True)
        row = FailureMemoryCase(
            case_id="fm_early_terminated_001",
            failure_type="early_terminated",
            summary="The agent stopped early.",
        ).model_dump(mode="json")
        (cases_dir / "cases.jsonl").write_text(
            json.dumps(row) + "\n" + json.dumps(row) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            storage.load_failure_memory()

    def test_save_and_load_screenshot(self) -> None:
        storage.save_trajectory(sample_run())
        data = b"\x89PNG\r\n\x1a\nfake-bytes"

        storage.save_screenshots("run_1", {"screenshot_001.png": data})
        loaded = storage.load_screenshot("run_1", "screenshot_001.png")

        self.assertEqual(loaded, data)
        self.assertTrue(storage.screenshot_exists("run_1", "screenshot_001.png"))
        self.assertEqual(storage.screenshot_content_type("run_1", "screenshot_001.png"), "image/png")

    def test_load_missing_screenshot_returns_none(self) -> None:
        self.assertIsNone(storage.load_screenshot("run_1", "missing.png"))

    def test_screenshot_path_traversal_rejected(self) -> None:
        storage.save_trajectory(sample_run())
        # _safe_id forbids "/" so this lookup must just return None, not escape.
        self.assertIsNone(storage.load_screenshot("run_1", "../../etc/passwd"))


if __name__ == "__main__":
    unittest.main()
