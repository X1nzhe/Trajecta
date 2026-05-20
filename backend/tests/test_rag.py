from app.rag import retrieve_similar_cases
from app.schemas import FailureMemoryCase


def test_retrieve_similar_cases_prefers_overlapping_terms():
    cases = [
        FailureMemoryCase(
            case_id="case_001",
            failure_label="action_failed",
            summary="Button disabled after modal",
            tags=["button", "modal"],
        ),
        FailureMemoryCase(
            case_id="case_002",
            failure_label="timing_issue",
            summary="Slow load state",
            tags=["timeout"],
        ),
    ]

    result = retrieve_similar_cases("button modal click failed", cases, top_k=1)
    assert len(result) == 1
    assert result[0].case_id == "case_001"
