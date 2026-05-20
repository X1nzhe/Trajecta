from app.eval_case_generator import generate_eval_case
from app.schemas import EvalAnalysis, FailureMemoryCase, TrajectoryRun


def test_generate_eval_case_includes_retrieved_case_ids():
    run = TrajectoryRun.model_validate(
        {
            "run_id": "run_001",
            "steps": [
                {
                    "step_id": "step_001",
                    "action": "click checkout",
                    "screenshot_path": "screenshots/step_001.png",
                }
            ],
        }
    )
    analysis = EvalAnalysis(
        failure_label="action_failed",
        confidence=0.8,
        reasoning="Checkout button not clickable",
    )
    retrieved = [
        FailureMemoryCase(
            case_id="case_001",
            failure_label="action_failed",
            summary="Button disabled",
            tags=["button"],
        )
    ]

    eval_case = generate_eval_case(run, run.steps[0], analysis, retrieved)
    assert eval_case.similar_case_ids == ["case_001"]
    assert eval_case.failure_label == "action_failed"
