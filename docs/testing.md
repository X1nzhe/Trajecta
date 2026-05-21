# Testing

## RAGAS Evaluation

Create `backend/app/ragas_eval.py`. RAGAS is run **manually** via `python -m backend.app.ragas_eval`; it is not integrated into pytest and not part of CI in v1. The script reads cached `last_trace.json` files (no live agent re-runs, no live retrieval) and writes the report files below.

Run one minimal RAGAS eval over failure memory RAG.

Preferred metrics:

- `faithfulness`
- `context_precision`

Input shape:

```python
{
  "question": "What failure pattern does this trajectory most closely match?",
  "answer": agent_generated_failure_analysis,
  "contexts": retrieved_context_texts_from_search_tool_results,
  "ground_truth": "missed_constraint"
}
```

Output files:

```text
eval/ragas_report.json
eval/ragas_report.md
```

If RAGAS setup is too slow, create a fallback script with a stub interface and document how to run real RAGAS.

## Pytest

Use pytest.

Required tests:

```text
tests/test_schema.py
- validate trajectory fixture schema
- reject missing run_id
- reject invalid step action type

tests/test_importer.py
- import at least 5 small MolmoWeb-HumanSkills sample or fixture runs
- convert raw sample to Trajecta JSON
- preserve screenshot path and raw action text

tests/test_coordinates.py
- validate coordinates when image dimensions are available
- mark invalid coordinates as out_of_bounds
- do not draw overlay for invalid coordinates
- do not draw bbox overlay unless bbox bounds are valid for the screenshot

tests/test_preprocess.py
- preprocess produces a trajectory_digest entry per step
- digest contains low-detail VLM summary, parsed action, and coordinate validation status
- preprocess uses deterministic mock VLM when no API key is configured

tests/test_tools.py
- get_run returns known run with attached digest
- get_run accepts a comparison run_id distinct from the run currently under analysis
- get_step_detail returns high-detail analysis for a valid step
- get_step_detail with image_detail="low" returns a low-detail analysis without throwing
- get_step_detail rejects a crop outside screenshot bounds
- find_similar_successful_run returns only runs with status=="success" and excludes the queried run_id
- find_similar_successful_run returns an empty list when no successful run is indexed for the task
- propose_eval_case rejects an EvalCase draft missing required fields

tests/test_eval_agent.py
- uses the Offline Agent Mock when no LLM credentials are configured
- agent terminates via propose_eval_case when evidence is sufficient
- agent terminates with budget_exceeded when tool-call budget is reached
- agent's retrieved_context_ids match IDs actually returned by search_* tool calls in the trace
- agent uses get_step_detail no more than min(tool_call_budget, ceil(0.3 * step_count)) times on run-level analysis

tests/test_api.py
- list runs endpoint returns at least 5 imported or fixture runs
- screenshot endpoint returns a fixture image by run_id and filename
- screenshot endpoint rejects missing files and path traversal
- analyze endpoint returns eval_case_draft and agent_trace
- analyze endpoint exposes tool_call_count and terminated_by inside agent_trace
- POST /api/eval-cases rejects human_validated=false with 422
- failure-memory and eval-case search endpoints return schema-valid result lists

tests/test_rag.py
- ChromaDB collection initializes
- failure memory seed contains at least 5 cases including missed_constraint
- search_failure_memory returns missed_constraint case for constraint query
- search_eval_cases defaults to human_validated=true cases
- successful_runs collection only indexes runs with status=="success"
- find_similar_successful_run returns higher similarity for same-task runs than for cross-task runs
- top_k length is respected

tests/test_eval_case.py
- agent eval_case_draft validates against the EvalCase contract
- exported eval case validates against the EvalCase contract
```

## Frontend Tests

Use Vitest or Playwright when the frontend exists.

```text
- ScreenshotViewer does not draw markers or bboxes unless validation allows it
- EvalAgentPanel renders propose_eval_case, budget_exceeded, and error termination states
- EvalCaseDraft requires human validation before export
```

## Acceptance Criteria

Project is complete when:

- `pytest` passes
- Backend starts locally
- Frontend starts locally
- At least 5 imported or fixture trajectory runs load
- User can select a run and step
- Screenshot and action details display
- Coordinate overlay is shown only when validated
- Trajectory Preprocessing produces a trajectory digest for any imported run
- Eval Agent autonomously inspects suspicious steps, retrieves similar cases, and terminates via `propose_eval_case`
- Per-run agent trace is written to `data/runs/{run_id}/last_trace.json` and rendered in the frontend
- ChromaDB retrieves similar failure cases and eval cases
- Eval case draft is generated as a fully-populated EvalCase JSON
- User can review, edit, and export the eval case
- RAGAS or fallback eval report exists at `eval/ragas_report.md`
- README clearly explains agent, tools, preprocessing, RAG, eval, tests, LangGraph, ChromaDB, tracing, and roadmap
