# Eval Agent

The Eval Agent is the core agent.

It should be implemented as a small LangGraph workflow.

The project description should emphasize the Eval Agent, not LangGraph. LangGraph is an implementation detail.

## Tools

Implement in `backend/app/tools.py`.

```python
def get_run(run_id: str) -> dict:
    """Return trajectory run metadata and steps."""


def get_step(run_id: str, step_index: int) -> dict:
    """Return one trajectory step."""


def get_screenshot_summary(run_id: str, step_index: int) -> dict:
    """Return a VLM-generated or mocked screenshot summary."""


def search_similar_cases(query: str, top_k: int = 3) -> list[dict]:
    """Retrieve similar failure memory cases from ChromaDB."""


def generate_eval_case(
    run_id: str,
    failure_step: int,
    failure_type: str,
    human_note: str | None = None
) -> dict:
    """Generate structured eval case draft."""
```

## LangGraph State

Create `backend/app/eval_agent_graph.py`.

```python
from typing import TypedDict, Optional, List, Dict, Any


class EvalState(TypedDict):
    run_id: str
    selected_step: Optional[int]
    run: Optional[Dict[str, Any]]
    relevant_steps: List[Dict[str, Any]]
    retrieved_cases: List[Dict[str, Any]]
    analysis: Optional[Dict[str, Any]]
    eval_case: Optional[Dict[str, Any]]
    errors: List[str]
```

## LangGraph Nodes

Use a small linear graph.

```text
START
-> load_run
-> select_relevant_steps
-> retrieve_similar_cases
-> analyze_trajectory
-> generate_eval_case
-> validate_output
-> END
```

## Agent Behavior

1. Load the run using `get_run`.
2. Select relevant steps.
3. If a selected step exists, inspect that step and nearby steps.
4. Use `search_similar_cases` to retrieve related failure memories.
5. Use LLM/VLM to produce structured analysis.
6. Generate eval case draft.
7. Validate eval case schema.
8. Return JSON only.

## Agent Output Schema

```json
{
  "suggested_failure_step": 3,
  "suggested_failure_type": "missed_constraint",
  "confidence": 0.78,
  "reason": "The agent selected an item before checking the user's stated constraint.",
  "evidence": [
    "Step 3 action clicked the first result.",
    "The task required a specific constraint that was not verified."
  ],
  "similar_cases": ["case_missed_constraint_001"],
  "eval_case_draft": {
    "expected_behavior": "The agent should verify all explicit constraints before selecting a final item.",
    "actual_behavior": "The agent selected a result before verifying constraints.",
    "regression_rule": "Pass only if the selected result satisfies the explicit constraints."
  }
}
```

## Skill

The Skill wrapper is optional packaging around the Eval Agent workflow. It is not a v1 blocker.

Create one skill file:

`skills/create-eval-case/SKILL.md`

```md
---
name: create-eval-case
description: Use when a browser-agent trajectory has a suspected or labeled failure and should be converted into a reusable regression eval case.
---

# Create Eval Case

## Inputs
- run_id
- failure_step
- failure_type
- optional human_note

## Procedure
1. Load the trajectory.
2. Inspect the failure step and nearby steps.
3. Retrieve similar failure memories.
4. Identify expected behavior, actual behavior, and evidence.
5. Generate structured eval_case JSON.
6. Require human validation before marking the case as final.

## Output
Return JSON with:
- task
- failure_step
- failure_type
- expected_behavior
- actual_behavior
- evidence
- regression_rule
```
