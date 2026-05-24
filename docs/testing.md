# Testing

## RAGAS Evaluation

Create `backend/app/ragas_eval.py`. RAGAS is run **manually** via `python -m backend.app.ragas_eval`; it is not integrated into pytest and not part of CI in v1. The script reads persisted `AgentTrace` records via `storage.load_trace(run_id)` (the `traces` SQLite table — no live agent re-runs, no live retrieval) and writes the report files below.

Run one minimal RAGAS eval over failure memory RAG.

Preferred metrics:

- `faithfulness`
- `context_precision`

Input shape:

```python
{
  "question": "What failure pattern does this trajectory most closely match?",
  "answer": ragas_answer_from_trace(trace),
  "contexts": retrieved_context_texts_from_search_tool_results,
  "ground_truth": "missed_constraint"
}
```

### `answer` derivation

The RAGAS `answer` field is built from the `propose_eval_case` tool call recorded in the trace. The agent's "failure analysis" is exactly what the agent passed to the terminal tool, so RAGAS scores faithfulness against the structured conclusion rather than any free-form intermediate `agent_message`.

```python
def ragas_answer_from_trace(trace: AgentTrace) -> str:
    """Extract the agent's failure analysis text from a persisted trace.

    Locates the **latest** `tool_call` event with name=="propose_eval_case"
    (a multi-turn trace may contain more than one) and concatenates the
    `actual_behavior` argument with each structured evidence `claim`. Raises
    if the trace did not terminate via propose_eval_case (e.g.
    terminated_by=="budget_exceeded" or "error") — such traces are skipped from
    the RAGAS sample.
    """
    calls = [
        e for e in trace.events
        if e.type == "tool_call" and e.name == "propose_eval_case"
    ]
    if not calls:
        raise ValueError("trace has no propose_eval_case tool call")
    args = calls[-1].args or {}
    actual_behavior = args["actual_behavior"]
    evidence = args.get("evidence", [])
    claims = [item["claim"] for item in evidence]
    return actual_behavior + "\n\n" + "\n".join(claims)
```

Rules:

- Only traces whose **latest turn** has `terminated_by == "propose_eval_case"` are included in the RAGAS sample; budget-exceeded and error terminations are filtered out at the script level and counted in the report.
- The answer text intentionally excludes `expected_behavior`, `regression_rule`, and `agent_message` events. `expected_behavior` describes the correct outcome (not the agent's claim about *this* run), and free-form `agent_message` text often contains discarded hypotheses that would inflate hallucination signal unfairly.
- `actual_behavior` and `evidence[*].claim` are read from the **trace** (the tool-call `args`), not from a persisted `EvalCase` file, because drafts are not persisted and the trace is the only source available to `ragas_eval.py` (see [docs/eval_agent.md](eval_agent.md) Observability section).
- `contexts` are accumulated from **all** `search_failure_memory` / `search_eval_cases` `tool_result` events in the trace, regardless of turn. A follow-up turn that retrieves additional evidence contributes to the same RAGAS sample as the initial turn's retrievals.

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
- find_similar_successful_run returns only runs with status=="success" and excludes the queried run_id
- find_similar_successful_run returns an empty list when no successful run is indexed for the task
- propose_eval_case rejects an EvalCase draft missing required fields

tests/test_eval_agent.py
- uses the Offline Agent Mock when no LLM credentials are configured
- agent terminates via propose_eval_case when evidence is sufficient
- agent terminates with budget_exceeded when tool-call budget is reached
- agent's retrieved_context_ids match IDs actually returned by search_* tool calls in the trace, across all turns
- agent's evidence items validate against `EvidenceItem`, and retrieval-derived evidence has `context_id` values returned by search_* tool calls
- step-detail evidence includes a `trace_event_seq` pointing to a matching `get_step_detail` event
- agent uses get_step_detail no more than min(tool_call_budget, ceil(0.3 * step_count)) times on run-level analysis
- AgentTraceEvent.seq is strictly monotonic across the whole trace, including across turns
- AgentTraceEvent.turn is non-decreasing across the event list
- a follow-up turn re-resumes the loop from the persisted messages and does not invoke the preprocess node again
- a follow-up turn that calls propose_eval_case produces a new draft; the trace contains two propose_eval_case tool calls and the latest one defines the current draft

tests/test_api.py
- list runs endpoint returns at least 5 imported or fixture runs
- screenshot endpoint returns a fixture image by run_id and filename
- screenshot endpoint rejects missing files and path traversal
- analyze endpoint returns an application/x-ndjson stream with at least one event line and a terminal done line
- analyze done line carries eval_case_draft and agent_trace; agent_trace exposes tool_call_count, turn_count, and terminated_by
- analyze streamed event.seq values are strictly increasing and start at 0
- followup endpoint returns 409 (as a single error response, not a stream) when no `traces` row exists for the run
- followup endpoint returns 422 when message is missing, empty, or > 2000 chars
- followup endpoint streams a user_message event with the next turn value as the first event line
- followup endpoint enforces its own per-turn budget (default 4) independent of the initial analyze
- followup endpoint streamed event.seq values start at prior_max_seq + 1
- followup endpoint with a propose_eval_case in the new turn produces a done line whose eval_case_draft replaces the previous one
- followup endpoint preserves the trace's original user_intent and selected_step
- POST /api/eval-cases rejects human_validated=false with 422
- failure-memory and eval-case search endpoints return schema-valid result lists

API tests that hit `/analyze`, `/steps/{i}/analyze`, or `/followup` must drain the NDJSON stream before asserting. Centralize this in a `drain_ndjson(response) -> tuple[list[dict], dict]` helper that returns `(event_lines, terminal_line)`. HTTP error responses (404 / 409 / 422) are **not** streamed — they return a regular JSON error body, so the helper must short-circuit when `status_code != 200`.

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
- eval_case_draft evidence validates as structured EvidenceItem rows
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
- Per-run agent trace is persisted as the `traces` SQLite row keyed by `run_id` (`storage.save_trace`) and rendered in the frontend
- ChromaDB retrieves similar failure cases and eval cases
- Eval case draft is generated as a fully-populated EvalCase JSON
- User can review, edit, and export the eval case
- RAGAS or fallback eval report exists at `eval/ragas_report.md`
- README clearly explains agent, tools, preprocessing, RAG, eval, tests, LangGraph, ChromaDB, tracing, and roadmap
