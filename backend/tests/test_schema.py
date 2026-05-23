from __future__ import annotations

import unittest

from pydantic import ValidationError

from backend.app.schemas import EvalCase, StepAction, TrajectoryRun


class SchemaContractTests(unittest.TestCase):
    def test_minimal_trajectory_run_validates(self) -> None:
        run = TrajectoryRun.model_validate(
            {
                "run_id": "fixture_run",
                "task": "navigate: find an example result",
                "steps": [
                    {
                        "index": 0,
                        "observation": {"screenshot": "screenshot_001.png"},
                        "action": {"type": "click", "coordinates": {"x": 10, "y": 20}},
                    }
                ],
            }
        )

        self.assertEqual(run.run_id, "fixture_run")
        self.assertEqual(run.steps[0].action.type, "click")
        self.assertEqual(run.steps[0].result.status, "unknown")

    def test_rejects_missing_run_id(self) -> None:
        with self.assertRaises(ValidationError):
            TrajectoryRun.model_validate(
                {
                    "task": "navigate: missing run id",
                    "steps": [
                        {
                            "index": 0,
                            "observation": {},
                            "action": {"type": "wait"},
                        }
                    ],
                }
            )

    def test_rejects_invalid_action_type(self) -> None:
        with self.assertRaises(ValidationError):
            StepAction.model_validate({"type": "double_click"})

    def test_eval_case_requires_valid_failure_type(self) -> None:
        with self.assertRaises(ValidationError):
            EvalCase.model_validate(
                {
                    "case_id": "ec_fixture_step_0",
                    "source_run_id": "fixture_run",
                    "task": "navigate: find an example result",
                    "failure_step": 0,
                    "failure_type": "Invalid Label",
                    "expected_behavior": "The agent should satisfy the task.",
                    "actual_behavior": "The agent did not satisfy the task.",
                    "evidence": [],
                    "regression_rule": "Reject invalid failure type labels.",
                }
            )


if __name__ == "__main__":
    unittest.main()
