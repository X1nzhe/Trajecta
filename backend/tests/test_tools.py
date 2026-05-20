from app.schemas import TrajectoryRun
from app.tools import analyze_step, get_step


def test_get_step_returns_expected_step():
    run = TrajectoryRun.model_validate(
        {
            "run_id": "run_001",
            "steps": [
                {
                    "step_id": "step_001",
                    "action": "click",
                    "screenshot_path": "screenshots/step_001.png",
                }
            ],
        }
    )
    step = get_step(run, "step_001")
    assert step.action == "click"


def test_analyze_step_flags_failed_step():
    run = TrajectoryRun.model_validate(
        {
            "run_id": "run_001",
            "steps": [
                {
                    "step_id": "step_002",
                    "action": "submit form",
                    "screenshot_path": "screenshots/step_002.png",
                    "success": False,
                    "error": "Button disabled",
                }
            ],
        }
    )
    result = analyze_step(run.steps[0])
    assert result.failure_label == "action_failed"
    assert result.confidence > 0.8
