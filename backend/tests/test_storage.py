from __future__ import annotations

import json
import os
import tempfile
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
    TrajectoryRun,
    TrajectoryStep,
)


def sample_run(run_id: str = "run_1", status: str = "unknown") -> TrajectoryRun:
    return TrajectoryRun(
        run_id=run_id,
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


def sample_eval_case(case_id: str = "ec_run_1_step_0") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        source_run_id="run_1",
        task="Find a result",
        failure_step=0,
        failure_type="early_terminated",
        expected_behavior="The agent should finish the task.",
        actual_behavior="The agent stopped before finishing.",
        evidence=[EvidenceItem(claim="Step 0 stopped.", source="trajectory", run_id="run_1", step_index=0)],
        regression_rule="Do not stop before task evidence is visible.",
        human_validated=True,
    )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get("TRAJECTA_DATA_DIR")
        os.environ["TRAJECTA_DATA_DIR"] = self.tmp.name

    def tearDown(self) -> None:
        if self.previous_data_dir is None:
            os.environ.pop("TRAJECTA_DATA_DIR", None)
        else:
            os.environ["TRAJECTA_DATA_DIR"] = self.previous_data_dir
        self.tmp.cleanup()

    def test_save_and_load_run(self) -> None:
        run = sample_run()

        storage.save_run(run)
        loaded = storage.load_run("run_1")

        self.assertEqual(loaded.run_id, "run_1")
        self.assertEqual(loaded.steps[0].action.type, "wait")

    def test_list_runs(self) -> None:
        storage.save_run(sample_run("run_b"))
        storage.save_run(sample_run("run_a"))

        self.assertEqual([run.run_id for run in storage.list_runs()], ["run_a", "run_b"])

    def test_load_missing_digest_returns_none(self) -> None:
        self.assertIsNone(storage.load_digest("run_1"))

    def test_save_and_load_trace(self) -> None:
        trace = AgentTrace(run_id="run_1", user_intent="analyze_run")

        storage.save_trace("run_1", trace)
        loaded = storage.load_trace("run_1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.run_id, "run_1")

    def test_save_and_load_digest(self) -> None:
        digest = TrajectoryDigest(run_id="run_1", task="Find a result", step_count=0, steps=[])

        storage.save_digest("run_1", digest)
        loaded = storage.load_digest("run_1")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.task, "Find a result")

    def test_save_eval_case_refuses_overwrite(self) -> None:
        case = sample_eval_case()

        storage.save_eval_case(case)

        with self.assertRaises(FileExistsError):
            storage.save_eval_case(case)

    def test_load_failure_memory_rejects_duplicate_case_id(self) -> None:
        cases_dir = storage.data_dir() / "failure_memory"
        cases_dir.mkdir(parents=True)
        row = FailureMemoryCase(
            case_id="fm_duplicate",
            failure_type="early_terminated",
            summary="The agent stopped early.",
        ).model_dump(mode="json")
        (cases_dir / "cases.jsonl").write_text(
            json.dumps(row) + "\n" + json.dumps(row) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(ValueError):
            storage.load_failure_memory()

    def test_writes_validate_through_pydantic_models(self) -> None:
        storage.save_run(sample_run())
        path = storage.data_dir() / "runs" / "run_1" / "trajectory.json"

        validated = TrajectoryRun.model_validate_json(path.read_text(encoding="utf-8"))

        self.assertEqual(validated.run_id, "run_1")


if __name__ == "__main__":
    unittest.main()
