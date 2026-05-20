import pytest
from pydantic import ValidationError

from app.schemas import TrajectoryRun


def test_trajectory_schema_parses_valid_payload():
    run = TrajectoryRun.model_validate(
        {
            "run_id": "run_001",
            "steps": [
                {
                    "step_id": "step_001",
                    "action": "click login",
                    "screenshot_path": "screenshots/step_001.png",
                    "success": True,
                }
            ],
        }
    )
    assert run.run_id == "run_001"
    assert run.steps[0].step_id == "step_001"


def test_schema_rejects_success_with_error():
    with pytest.raises(ValidationError):
        TrajectoryRun.model_validate(
            {
                "run_id": "run_001",
                "steps": [
                    {
                        "step_id": "step_001",
                        "action": "click login",
                        "screenshot_path": "screenshots/step_001.png",
                        "success": True,
                        "error": "bad",
                    }
                ],
            }
        )
