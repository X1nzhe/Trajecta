# Testing

Trajecta's eval surface has four pillars. Each maps to a specific S18
§ 2.2 build requirement and to a deliverable in
[`docs/phase8_s18_alignment.md`](phase8_s18_alignment.md).

| Surface | Artefact | S18 § |
| --- | --- | --- |
| Golden set | `eval/golden.jsonl`, 35 cases | 2.2 Build 1 |
| Deterministic unit suite | `backend/tests/`, OfflineAgentMock | 2.2 Build 2 |
| Semantic metric | `eval/ragas_report.{json,md}`, no-ground-truth RAGAS faithfulness | 2.2 Build 3 |
| LLM judge + κ | `eval/judge.py`, `eval/runs/{ts}/judge/judge_agreement_report.{json,md}` | 2.2 Build 4 |

## Golden Set

**File**: `eval/golden.jsonl`, JSONL, 35 rows.

**Per-row schema** (facts are structured objects, not free-text strings,
so the judge can run mechanical prechecks without a regex parser):

```json
{
  "input": {
    "run_id": "87ea181f...",
    "intent": "analyze_run"
  },
  "expected_facts": [
    {"field": "outcome",      "op": "eq",       "value": "failed"},
    {"field": "failure_type", "op": "in",       "value": ["missed_constraint"]},
    {"field": "failure_step", "op": "in_range", "value": [10, 14]}
  ],
  "forbidden_facts": [
    {"field": "outcome",      "op": "eq", "value": "success"},
    {"field": "failure_type", "op": "in", "value": ["early_terminated", "wrong_target", "wrong_result", "inefficient_search"]}
  ],
  "tags": ["booking", "missed_constraint"]
}
```

**Fact shape** (Pydantic discriminated union on `field`):

| `field` | `op` | `value` type | Semantics |
| --- | --- | --- | --- |
| `outcome` | `eq` | `"success" \| "failed"` | Proposed verdict matches this outcome literal. |
| `failure_type` | `in` | `list[str]` (subset of `V1_FAILURE_VOCABULARY`) | Proposed `failure_type` is one of the listed types. |
| `failure_step` | `in_range` | `[int, int]` (inclusive `[min, max]`, `min ≤ max`) | Proposed `failure_step` lies in this closed interval. |

`expected_facts` are conditions the proposed `EvalCase` **must** satisfy.
`forbidden_facts` are conditions it **must not** satisfy. The judge uses
these structured facts as deterministic prechecks and as compact context
for the LLM acceptability decision (§ LLM Judge).

**Source of truth**: `data/triage_notes.csv`. The CSV carries curated
annotations and is hand-edited; `eval/golden.jsonl` is a build artefact
produced by `scripts/build_golden_jsonl.py` and never edited by hand.

**Build rules**:

- `input.run_id` ← CSV `sample_id`. `input.intent` defaults to `"analyze_run"`.
- For `outcome=="success"` rows:
  - `expected_facts = [{outcome eq "success"}]`
  - `forbidden_facts = [{outcome eq "failed"}]`
  - `tags = [category]`
- For `outcome=="failed"` rows:
  - `expected_facts = [{outcome eq "failed"}, {failure_type in labelled_set}]`
    plus `{failure_step in_range [step − 2, step + 2]}` when `failure_step` is non-empty.
  - `forbidden_facts = [{outcome eq "success"}, {failure_type in (V1_FAILURE_VOCABULARY \ labelled_set)}]`.
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

**File**: `eval/judge.py`, invoked by `backend.app.agent_eval` after the
agent-quality eval finishes. It is also runnable as `python -m eval.judge`
to rerun the judge against an existing `agent_report.json` + trace
directory.

The judge scores one quality dimension — **`acceptable_eval_case`**,
binary — over the generated `eval_case_draft` for each golden case. The
judged object is the latest `propose_eval_case` tool-call args in the
persisted `AgentTrace`.

The judge is **not** scoring "evidence traceability" as its rubric.
Evidence support is one assertion inside the broader question: is this
draft acceptable as a reusable regression eval case for the run?

### Acceptability Assertions

The Phase 8 protocol runs two LLM judges over the same case payload:

- Judge A: Gemini-compatible provider/model configured by
  `TRAJECTA_JUDGE_A_MODEL`.
- Judge B: OpenAI-compatible provider/model configured by
  `TRAJECTA_JUDGE_B_MODEL`.
- Judge prompt versions are configured by
  `TRAJECTA_JUDGE_A_PROMPT_VERSION` and
  `TRAJECTA_JUDGE_B_PROMPT_VERSION`.

Both judges return `acceptable` or `unacceptable` plus assertion results.
A draft is acceptable iff all assertions pass. No Gemini or OpenAI model ID is
hard-coded as a repo default. The two prompt versions must keep the same rubric
semantics; provider-specific formatting and instruction wording are allowed,
but the acceptability criteria must remain equivalent.

Phase 8 A4.2 ships two provider-specific prompt bundles for the
production judge pair:

- `prompts/judge/v1_acceptability_gemini/` — Judge A default.
- `prompts/judge/v1_acceptability_openai/` — Judge B default.

Both bundles list the six required assertion names below verbatim and
demand JSON-only output. `backend/tests/test_judge.py` locks in
existence, assertion-name coverage, and distinct sha256 stamps for the
two bundles so a future edit that breaks rubric alignment fails CI
before the κ_LLM,LLM rollup is computed.

| Assertion | Predicate |
| --- | --- |
| Verdict alignment | The draft's success/failure shape matches the golden `OutcomeFact`. |
| Failure-mode compatibility | For failed references, `failure_type` is compatible with the labelled failure-type set. |
| Failure-step localization | For failed references with a labelled step, `failure_step` is inside the expected range, or the cited evidence demonstrates that the inspected step still covers the labelled failure. |
| Regression-case usefulness | `expected_behavior`, `actual_behavior`, and `regression_rule` would let a future regression eval catch the same failure. |
| No forbidden claim | The draft does not assert any `forbidden_facts` entry. |
| Evidence support | The cited evidence supports the draft's claim; missing screenshots, invalid coordinates, or unavailable sources are represented as honest gaps rather than invented evidence. |

`eval/judge.py` may precompute deterministic checks from
`expected_facts` / `forbidden_facts` to keep the prompt compact and make
failure reporting reproducible. Those checks are preconditions and
context for the LLM judge, not a replacement for the final
`acceptable_eval_case` verdict.

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
  "assertions": [
    {
      "name": "verdict_alignment",
      "status": "pass",
      "rationale": "<one short sentence>"
    }
  ]
}
```

`verdict` is `"acceptable"` or `"unacceptable"`. Every assertion has
`status: "pass" | "fail"` and a short rationale.

### Required CLI shape

```text
python -m backend.app.agent_eval \
    --trace-dir eval/runs/{timestamp}/traces \
    --judge

# Rerun/debug path for a single configured slot:
python -m eval.judge \
    --golden eval/golden.jsonl \
    --report eval/agent_report.json \
    --trace-dir eval/runs/{timestamp}/traces \
    --out eval/judge_report.json
```

Judge model and prompt-version selection is controlled by environment
variables. Model values below are placeholders / examples only, not repo
defaults:

```text
TRAJECTA_JUDGE_A_MODEL=<gemini-model-id>
TRAJECTA_JUDGE_A_PROMPT_VERSION=<judge-a-prompt-version>
TRAJECTA_JUDGE_B_MODEL=<openai-model-id>
TRAJECTA_JUDGE_B_PROMPT_VERSION=<judge-b-prompt-version>
```

Standalone reruns read one configured slot at a time. The production
`agent_eval --judge` path runs the configured A/B slots and writes the
agreement artefact under the timestamped eval archive.

### Outputs

- Production post-step:
  `eval/runs/{timestamp}/judge/judge_agreement_report.{json,md}` —
  κ_LLM,LLM across the successful Judge A/B slot reports.
- Per-slot reports:
  `eval/runs/{timestamp}/judge/{A,B}/judge_report.{json,md}` —
  per-case verdicts, acceptability assertions, and aggregate
  `acceptable_rate` for one judge.
- Standalone rerun/debug:
  `eval/judge_report.{json,md}` when `python -m eval.judge --out
  eval/judge_report.json` is used for one configured slot.

## Cohen's κ

Compute Cohen's κ over the binary verdicts for the same set of cases judged
by the Gemini LLM judge and the OpenAI LLM judge.

```python
# Pseudocode
p_observed = sum(a == b for a, b in zip(A, B)) / N
p_a_positive = sum(A) / N
p_b_positive = sum(B) / N
p_expected = (p_a_positive * p_b_positive
              + (1 - p_a_positive) * (1 - p_b_positive))
kappa = (p_observed - p_expected) / (1 - p_expected)
```

### One κ row

**κ_LLM,LLM**. κ is computed between the Gemini and OpenAI binary
`acceptable_eval_case` verdicts, N = 31 on the current gradeable golden
set. This is the Phase 8 primary agreement deliverable.

The golden set is reference context for both judges. It is not a third
annotator and must not be converted into verdicts.

### Sample policy

Preferred judge N = 31 gradeable cases from the 35-row golden set. When cost
is constrained, a judge run may use a deterministic pre-registered stratified
subset. The judge report must disclose `sample_size`, `selection_policy`, and
skipped counts. `eval/golden.jsonl` remains 35 rows; do not shrink the golden
set to control judge cost.

### Target and fallback

The S18 target is **κ ≥ 0.6**.

If κ falls below 0.6, the report does **not** silently relax the judge
contract to lift the number. Instead, the report adds a "Disagreement
Analysis" section listing every case the Gemini and OpenAI judges split on,
the acceptability assertions each judge failed, and a one-line hypothesis
about the source of disagreement (prompt ambiguity vs model behavior vs
genuinely hard sample). Per S18 § 2.3 closing note: "a negative result is
still a result".

Reviewer validation can be added later as a separate confidence check, but it
is deferred and not part of the Phase 8 acceptance path because reviewer
workflow, UI, and label-management design would add implementation scope beyond
Phase 8.

## RAGAS Evaluation

Create `backend/app/ragas_eval.py`. RAGAS is run **manually** via `python -m backend.app.ragas_eval`; it is not integrated into pytest and not part of CI in v1. The script reads persisted `AgentTrace` records from an explicit `--trace-dir` first, then falls back to `storage.load_trace(run_id)` from the `traces` SQLite table. It does not re-run the agent or retrieval.

Run one minimal no-ground-truth RAGAS eval over failure memory RAG.

Primary metric:

- `faithfulness`

Input shape:

```python
{
  "question": search_tool_call.args["query"],
  "answer": ragas_answer_from_trace(trace),
  "contexts": matching_search_tool_result_items_as_text,
  "ground_truth_source": "none"
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

- Only traces whose **latest turn** has `terminated_by == "propose_eval_case"` can contribute RAGAS samples; budget-exceeded and error terminations are filtered out at the script level and counted in the report.
- The answer text intentionally excludes `expected_behavior`, `regression_rule`, and `agent_message` events. `expected_behavior` describes the correct outcome (not the agent's claim about *this* run), and free-form `agent_message` text often contains discarded hypotheses that would inflate hallucination signal unfairly.
- `actual_behavior` and `evidence[*].claim` are read from the **trace** (the tool-call `args`), not from a persisted `EvalCase` file, because drafts are not persisted and the trace is the only source available to `ragas_eval.py` (see [docs/eval_agent.md](eval_agent.md) Observability section).
- Each RAGAS sample corresponds to one recorded `search_failure_memory` or `search_eval_cases` tool call. `question` is that tool call's `args["query"]`; `contexts` are the matching following `tool_result.items`, not a cross-trace or whole-trace context pool.
- No human or self-generated `ground_truth` is used. The A6 claim is limited to retrieval-grounded faithfulness: whether the final `actual_behavior` and evidence claims are supported by the contexts retrieved for the recorded query. It does not measure answer correctness, context recall, or human agreement.
- RAG tool calls with no usable contexts are skipped and counted under `no_context`.

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
  even when `OPENAI_API_KEY` is set. Phase 8 reads from the explicit
  eval-harness trace dump dir (`eval/runs/{ts}/traces/`, see Phase 8 A2)
  first, then falls back to the SQLite `traces` table.
- Run against ≥ 10 real RAG tool-call samples from the most recent golden-set evaluation.
- `eval/ragas_report.md` `mode` field must read `"real"`, not `"stub"`.
- The S18 § 2.2 Build 3 requirement is satisfied by `faithfulness`
  alone; no `ground_truth` or `context_precision` claim is made for A6.

Latest Phase 8 A6 artefact: `eval/ragas_report.{json,md}` was generated
from `eval/runs/2026-05-30T04-43-34Z/traces` with `--limit 10`; it reports
`ragas_mode="real"`, `ground_truth_source="none"`, sample count 10,
`faithfulness=0.4068`, and skipped counts
`budget_exceeded=0`, `error=7`, `no_trace=4`, `no_context=17`.

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
- judge extracts the latest eval_case_draft from each agent_eval trace
- mechanical prechecks produce reproducible assertion context
- judge_report.json stores per-slot verdicts plus acceptability assertions
- judge_agreement_report.json carries the κ_LLM,LLM row for Gemini vs OpenAI
- Cohen's κ matches a hand-computed value on a fixture Gemini/OpenAI verdict pair
- disagreement-analysis section renders when κ < 0.6
- judge does not synthesize verdicts from golden references

tests/test_agent_eval.py            (extend)
- --trace-dir flag dumps one per-sample trace JSON under the given dir
- dumped trace contains the propose_eval_case args and the full evidence list
- the dump path defaults to eval/runs/{ts}/traces/ when the flag is omitted
- retryable 429 / timeout / connection failures are retried per sample
- non-retryable agent errors are not retried and still count as agent_error
- existing trace_dir/{run_id}.json files resume directly into grading without calling _run_agent
- resume rejects prompt_version mismatches to prevent cross-prompt contamination
- explicit eval/runs/{ts}/traces resume writes the final report back to eval/runs/{ts}/
- judge post-step receives the same report path and trace dir produced by the eval run
- judge post-step runs env-configured Gemini-compatible and OpenAI-compatible judge configs with different committed judge prompt versions

tests/test_ragas_eval.py            (extend)
- path resolver prefers the explicit eval-trace dump dir over SQLite when both exist
- path resolver falls back to the SQLite traces table when no trace-dir file exists
- samples use real RAG tool-call queries and matching tool-result contexts
- `ground_truth_source` is `none`; disk fixtures do not turn A6 into answer correctness
- retrieval calls without usable contexts increment `no_context`
- `--limit` restricts valid sample count and is threaded through the CLI
- mode field on the produced report is "real" when OPENAI_API_KEY is set and at least one trace is loadable
- mode field falls back to "stub" only when OPENAI_API_KEY is unset

tests/test_prompts.py               (Phase 8 B6 Spotlighting hardening)
- spotlight_wrap() returns the same delimiter token within one agent run and different tokens across runs
- spotlight_wrap() of an empty string still emits a valid delimited pair; off-mode is identity; missing token raises
- spotlighting_enabled() parses TRAJECTA_SPOTLIGHTING (default on) and rejects unknown values
- load_prompt_bundle prepends the anti-injection preamble when on; system + combined sha256 differ between on/off

tests/test_eval_agent.py::SpotlightingWrapTests   (Phase 8 B6 Spotlighting hardening)
- the initial digest HumanMessage wraps action_text, action_target, URL, title, and VLM low-detail summary in `<TRAJECTA_DATA_*>` markers
- get_step_detail wraps vlm_summary, task_context, and observation text; the trusted run.task stays unwrapped
- internal RAG retrieval results and agent message history are NOT wrapped
- on/off runs stamp different prompt_sha256 and AgentTrace.spotlighting_enabled; followup re-mints a fresh token

Spotlighting is unit-tested production hardening only — there is no injection golden set or `injection_resistance_rate` eval in Phase 8.

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
- `eval/runs/{ts}/judge/judge_agreement_report.md` — κ_LLM,LLM row present with N=31 preferred, or a reported deterministic stratified subset; if κ < 0.6, disagreement analysis section present.
- `eval/ragas_report.md` — `mode == "real"`, `n ≥ 10`.
- `README.md` — "Eval & Experiments" table ≥ 5 rows with concrete metric deltas (no "improved slightly" phrasing).
- `docs/failure_analysis.md` — 2–3 case studies + one-line trade-off.
- Planned lower-priority `mcp/server.py` — six tools exposed, zero excluded tools registered, `analyze_run` composite present once the MCP slice ships.
- `cd frontend && npm run build` — exits 0.
- `git status` — clean.
- `PROJECT.md`, `README.md`, `docs/roadmap.md`, `docs/testing.md`, `docs/eval_agent.md` — all reflect Phase 8.
