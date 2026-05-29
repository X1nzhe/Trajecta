# Testing

Trajecta's eval surface has four pillars. Each maps to a specific S18
§ 2.2 build requirement and to a deliverable in
[`docs/phase8_s18_alignment.md`](phase8_s18_alignment.md).

| Surface | Artefact | S18 § |
| --- | --- | --- |
| Golden set | `eval/golden.jsonl`, 35 cases | 2.2 Build 1 |
| Deterministic unit suite | `backend/tests/`, OfflineAgentMock | 2.2 Build 2 |
| Semantic metric | `eval/ragas_report.{json,md}`, faithfulness + context_precision | 2.2 Build 3 |
| LLM judge + κ | `eval/judge.py`, `eval/judge_report.{json,md}`, `data/human_judge_labels.jsonl` | 2.2 Build 4 |

## Golden Set

**File**: `eval/golden.jsonl`, JSONL, 35 rows.

**Per-row schema**:

```json
{
  "input": {
    "run_id": "87ea181f...",
    "intent": "analyze_run"
  },
  "expected_facts": [
    "outcome == 'failed'",
    "failure_type ∈ {missed_constraint}",
    "failure_step ∈ [10, 14]"
  ],
  "forbidden_facts": [
    "outcome == 'success'",
    "failure_type ∈ {early_terminated, wrong_target, wrong_result, inefficient_search}"
  ],
  "tags": ["booking", "missed_constraint"]
}
```

**Source of truth**: `data/triage_notes.csv`. The CSV carries the human
labels and is hand-edited; `eval/golden.jsonl` is a build artefact
produced by `scripts/build_golden_jsonl.py` and never edited by hand.

**Build rules**:

- `input.run_id` ← CSV `sample_id`. `input.intent` defaults to `"analyze_run"`.
- For `outcome=="success"` rows:
  - `expected_facts = ["outcome == 'success'"]`
  - `forbidden_facts = ["outcome == 'failed'"]`
  - `tags = [category]`
- For `outcome=="failed"` rows:
  - `expected_facts = ["outcome == 'failed'", f"failure_type ∈ {labelled_set}"]`
    plus `f"failure_step ∈ [{step − 2}, {step + 2}]"` when `failure_step` is non-empty.
  - `forbidden_facts = ["outcome == 'success'",
    f"failure_type ∈ {V1_FAILURE_VOCABULARY \\ labelled_set}"]`.
  - `tags = [category, *labelled_set]`.

**Pydantic model**: `GoldenCase` in `backend/app/schemas.py` (added in
Phase 8). Each row validates on load.

**Acceptance**:

- 35 rows present.
- All eight category tags present (`allrecipes`, `amazon`, `apple`,
  `arxiv`, `booking`, `github`, `google_flight`, `huggingface`).
- `scripts/build_golden_jsonl.py --check` exits non-zero when
  `triage_notes.csv` was modified after `golden.jsonl`. Wired into CI as
  a soft gate.

## LLM Judge

**File**: `eval/judge.py`, runnable as `python -m eval.judge`.

The judge scores one quality dimension — **`acceptable_eval_case`**,
binary — over the proposed `EvalCase` for each golden case. Acceptance
is defined by the six-clause rubric below; a case is `acceptable` iff
**all six clauses hold**.

### Rubric

| # | Clause | Predicate |
| --- | --- | --- |
| 1 | Verdict match | Proposed `is_success` (= all five failure fields absent) matches the reference `outcome == "success"`. |
| 2 | Failure-type compatibility | For failed references, the proposed `failure_type` appears in the reference's `expected_facts` failure-type set (multi-label OR). |
| 3 | Failure-step locality | For failed references with a labelled step, the proposed `failure_step` lies in `[step − 2, step + 2]`, or proposed evidence demonstrates that the inspection covered the labelled step. |
| 4 | No contradiction with expected facts | Proposed `expected_behavior` and `actual_behavior` do not contradict any `expected_facts` entry. |
| 5 | No forbidden assertions | Proposed `expected_behavior`, `actual_behavior`, or any evidence claim does not assert any `forbidden_facts` entry. |
| 6 | Evidence traceability | Every `EvidenceItem` carries enough pointers (`step_index` for step-based sources, `context_id` for retrieval-based sources, or `source="unavailable"`) to locate or honestly disclaim the cited source. |

### Input shape (per case)

The judge harness pre-resolves the source content for each
`EvidenceItem` from the persisted trace + storage so the LLM never has
to call back to Trajecta:

```python
{
  "run_id": "...",
  "golden_reference": {<row from eval/golden.jsonl>},
  "proposed_eval_case": {<args of latest propose_eval_case tool_call>},
  "evidence_with_sources": [
    {"evidence": <EvidenceItem>,
     "resolved_source": <step JSON | failure_memory case | step_detail tool_result>}
    ...
  ]
}
```

### Output shape (per case)

```json
{
  "verdict": "acceptable",
  "rationale": "<≤2 sentences>",
  "failed_rubrics": [1, 3, 5]
}
```

`failed_rubrics` is empty when the verdict is `acceptable`.

### CLI

```text
python -m eval.judge \
    --golden eval/golden.jsonl \
    --report eval/agent_report.json \
    --trace-dir eval/runs/{timestamp}/traces \
    --judge-model claude-opus-4-1 \
    [--human-labels data/human_judge_labels.jsonl] \
    [--sample-size 31] \
    --out eval/judge_report.json
```

### Outputs

- `eval/judge_report.json` — per-case verdicts + aggregate
  `acceptable_rate` + the Cohen's κ tables.
- `eval/judge_report.md` — human-readable summary modelled on
  `eval/agent_report.md`.

## Cohen's κ

Compute Cohen's κ over the binary verdicts for the same set of cases
judged by two annotators.

```python
# Pseudocode
p_observed = sum(a == b for a, b in zip(A, B)) / N
p_a_positive = sum(A) / N
p_b_positive = sum(B) / N
p_expected = (p_a_positive * p_b_positive
              + (1 - p_a_positive) * (1 - p_b_positive))
kappa = (p_observed - p_expected) / (1 - p_expected)
```

### Two κ rows

**κ_LLM,LLM**. Run the judge twice with two different LLMs (e.g.
`claude-opus-4-1` and `gpt-4o-2024-08-06`). The κ value is computed
over their per-case verdicts, N = 31. This is the primary κ row in the
report.

**κ_LLM,human**. The user labels every gradeable golden case (binary
`acceptable_eval_case` + one-sentence rationale) and saves the result
to `data/human_judge_labels.jsonl`. κ is then computed between the
best-performing LLM judge and the human, N = 31.

### Target and fallback

The S18 target is **κ ≥ 0.6**.

If a κ row falls below 0.6, the report does **not** silently relax the
rubric to lift the number. Instead, the report adds a "Disagreement
Analysis" section listing every case the two annotators split on, the
rubric clauses each annotator failed, and a one-line hypothesis about
the source of disagreement (rubric ambiguity vs annotator error vs
genuinely hard sample). Per S18 § 2.3 closing note: "a negative result
is still a result".

### Human label collection

**File**: `data/human_judge_labels.jsonl`.

**Per-row schema**:

```json
{
  "run_id": "...",
  "human_verdict": "acceptable" | "not_acceptable",
  "rationale": "<1 sentence>"
}
```

The labelling UI is a CLI mode of `eval/judge.py`:

```text
python -m eval.judge --human-label-mode \
    --golden eval/golden.jsonl \
    --report eval/agent_report.json \
    --trace-dir eval/runs/{timestamp}/traces \
    --out data/human_judge_labels.jsonl
```

It prints, per case, the golden reference and the proposed `EvalCase`
side by side, prompts for verdict + rationale, and appends to the
output file. No React work.

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

### Phase 8 status

Phase 7 shipped a stub fallback for cold-start demos. Phase 8 A6 makes
real RAGAS the deliverable:

- Fix the path-resolution bug in `backend/app/ragas_eval.py`. The Phase
  7 version reads pre-storage-refactor paths and falls back to stub mode
  even when `OPENAI_API_KEY` is set. Phase 8 reads from the SQLite
  `traces` table when present and from the eval-harness trace dump dir
  (`eval/runs/{ts}/traces/`, see Phase 8 A2) otherwise.
- Run against ≥ 10 traces from the most recent golden-set evaluation.
- `eval/ragas_report.md` `mode` field must read `"real"`, not `"stub"`.
- The S18 § 2.2 Build 3 requirement is satisfied by `faithfulness`
  alone; `context_precision` is reported as a secondary signal.

The stub-mode fallback remains in the code for offline development but
is no longer an acceptable production artefact.

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
- followup endpoint enforces its own per-turn budget (default 8) independent of the initial analyze
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

### Phase 8 additions

```text
tests/test_golden_set.py            (new)
- build_golden_jsonl produces 35 rows from data/triage_notes.csv
- every row validates against the GoldenCase Pydantic model
- expected_facts and forbidden_facts are disjoint for every row
- all 8 categories appear in the tag column
- --check exits non-zero when triage_notes.csv is newer than golden.jsonl

tests/test_judge.py                 (new)
- judge invokes one of the six rubric clauses for every gold + draft pair
- failed_rubrics is empty iff verdict is "acceptable"
- judge_report.json carries the κ tables when two annotators are supplied
- Cohen's κ matches a hand-computed value on a fixture pair of annotators
- disagreement-analysis section renders when κ < 0.6

tests/test_agent_eval.py            (extend)
- --trace-dir flag dumps one per-sample trace JSON under the given dir
- dumped trace contains the propose_eval_case args and the full evidence list
- the dump path defaults to eval/runs/{ts}/traces/ when the flag is omitted

tests/test_ragas_eval.py            (extend)
- path resolver reads from the SQLite traces table when a row exists
- path resolver falls back to the eval-trace dump dir when no SQLite row exists
- mode field on the produced report is "real" when OPENAI_API_KEY is set and at least one trace is loadable
- mode field falls back to "stub" only when OPENAI_API_KEY is unset

tests/test_spotlight.py             (new, Phase 8 B6)
- spotlight_wrap() returns the same delimiter token within one agent run and different tokens across runs
- spotlight_wrap() of an empty string still emits a valid delimited pair
- trajectory_digest assembly wraps every StepObservation.visible_text, action_target, URL, and VLM text output
- internal RAG retrieval results and agent message history are NOT wrapped
- active system prompt contains the anti-injection preamble; sha256 stamp on the trace reflects it

tests/test_injection_eval.py        (new, Phase 8 B6)
- eval/injection_golden.jsonl validates against the GoldenCase schema and has ≥ 8 entries
- injection_resistance_rate computation matches a hand-counted reference on a fixture
- baseline-disabled run records injection_followed=True at least once on the crafted set (sanity: the eval is doing something)
- Spotlighting-enabled run records strictly higher injection_resistance_rate than baseline on the same fixture set
```

## Frontend Tests

Use Vitest or Playwright when the frontend exists.

```text
- ScreenshotViewer does not draw markers or bboxes unless validation allows it
- EvalAgentPanel renders propose_eval_case, budget_exceeded, and error termination states
- EvalCaseDraft requires human validation before export
```

## Acceptance Criteria

### v1 MVP (Phase 1–7)

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
- README clearly explains agent, tools, preprocessing, RAG, eval, tests, LangGraph, ChromaDB, tracing, and roadmap

### S18 capstone (Phase 8)

Per [`docs/phase8_s18_alignment.md`](phase8_s18_alignment.md) "Acceptance
Checklist":

- `eval/golden.jsonl` — 35 rows, schema-valid, all 8 categories present.
- `eval/runs/{ts}/traces/` — per-sample trace JSONs from the last eval run (local-only).
- `eval/judge_report.md` — κ_LLM,LLM row present with N=31; if κ < 0.6, disagreement analysis section present.
- `eval/judge_report.md` — κ_LLM,human row present with N=31.
- `data/human_judge_labels.jsonl` — 31 rows; every row carries a rationale.
- `eval/ragas_report.md` — `mode == "real"`, `n ≥ 10`.
- `README.md` — "Eval & Experiments" table ≥ 5 rows with concrete metric deltas (no "improved slightly" phrasing).
- `docs/failure_analysis.md` — 2–3 case studies + one-line trade-off.
- `mcp/server.py` — six tools exposed, zero excluded tools registered, `analyze_run` composite present.
- `cd frontend && npm run build` — exits 0.
- `git status` — clean.
- `SPEC.md`, `README.md`, `docs/roadmap.md`, `docs/testing.md`, `docs/eval_agent.md` — all reflect Phase 8.
