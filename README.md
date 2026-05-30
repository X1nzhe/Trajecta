# Trajecta

Trajecta turns raw browser-agent trajectories into human-validated regression eval cases.

Trajecta is an AI-native Eval Agent for browser-agent trajectory evaluation. It imports existing trajectory runs, replays screenshots and actions, uses a LangGraph tool-calling agent to inspect suspicious steps, retrieves similar failures from ChromaDB, and produces eval case drafts that humans review before export.

This is not a browser-use agent. It does not control a live browser in v1.

## Architecture

```text
MolmoWeb sample fixtures
        |
        v
TrajectoryRun JSON + screenshots
        |
        v
Trajectory Preprocessing
- parse actions
- validate coordinates
- low-detail VLM digest
        |
        v
LangGraph Eval Agent
- get_run
- get_step_detail
- find_similar_successful_run
- search_failure_memory
- search_eval_cases
- propose_eval_case
        |
        v
Human validation -> eval_case.json
```

Core contracts live in [docs/contracts.md](docs/contracts.md). Behavior docs live in [docs/preprocessing.md](docs/preprocessing.md), [docs/eval_agent.md](docs/eval_agent.md), [docs/rag.md](docs/rag.md), and [docs/api.md](docs/api.md).

## Demo Flow

1. Load at least 5 fixture runs derived from `allenai/MolmoWeb-HumanSkills`.
2. Select a run in the frontend.
3. Review screenshots, actions, results, and coordinate validation.
4. Click `Analyze Run` or `Analyze Selected Step`.
5. The Eval Agent inspects selected or suspicious steps, retrieves similar cases, and terminates through `propose_eval_case`.
6. Review the agent trace and eval case draft.
7. Confirm or edit the draft, then export the final eval case.

## Configuration

Local configuration is read from environment variables. You may use `.env` at the repo root; do not commit secrets.

```text
# Required for the real-LLM agent + VLM paths.
OPENAI_API_KEY=sk-...

# Tool-calling Eval Agent (LangChain ChatOpenAI). Without this, the
# agent falls back to OfflineAgentMock (deterministic, no network).
TRAJECTA_AGENT_MODEL=gpt-4o-mini

# Trajectory Preprocessing low-detail VLM + get_step_detail high-detail
# VLM. Without this, both VLM paths fall back to MockVLMClient.
TRAJECTA_VLM_MODEL=gpt-4o-mini

# Optional: versioned Eval Agent prompt bundle under prompts/eval_agent/.
# Unset defaults to v1_minimal. Each trace/report records version + hash.
TRAJECTA_PROMPT_VERSION=v1_minimal

# Optional: versioned high-detail VLM prompt under prompts/vlm_high_detail/.
# Unset defaults to v1_task_context. High-detail get_step_detail results
# and eval reports record version + hash.
TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION=v1_task_context

# Optional: Phase 8 dual LLM judge config. The repo does not hard-code
# judge model defaults; operators choose concrete model IDs.
TRAJECTA_JUDGE_A_MODEL=<gemini-model-id>
TRAJECTA_JUDGE_A_PROMPT_VERSION=<judge-a-prompt-version>
TRAJECTA_JUDGE_B_MODEL=<openai-model-id>
TRAJECTA_JUDGE_B_PROMPT_VERSION=<judge-b-prompt-version>

# Optional: ChromaDB embedding model. Falls back to chromadb's default
# sentence-transformers if unset. Changing this requires clearing
# data/chroma/ to rebuild the index — collections are not migrated.
TRAJECTA_EMBEDDING_MODEL=text-embedding-3-small
```

Prompt updates are versioned directories under `prompts/eval_agent/`,
`prompts/vlm_high_detail/`, and `prompts/judge/`. Create a new directory for
each prompt change and roll back by setting the corresponding environment
variable to a previous version. See
[docs/prompt_versioning.md](docs/prompt_versioning.md).

**Fallback behavior** — with **no** env vars set, the backend boots
successfully and `/api/runs/{id}/analyze` runs against:
- `OfflineAgentMock` for the agent (5-stage deterministic script)
- `MockVLMClient` for both low-detail preprocessing and high-detail step inspection (deterministic hash-derived summaries)

This is the path the default pytest suite exercises. To smoke-test the
real LLM path end-to-end:

```bash
OPENAI_API_KEY=sk-... TRAJECTA_AGENT_MODEL=gpt-4o-mini TRAJECTA_VLM_MODEL=gpt-4o-mini \
  pytest backend/tests/test_real_llm_integration.py -v
```

This test costs real OpenAI tokens; it's opt-in and not part of CI.

## Run Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## Run Frontend

```bash
cd frontend
npm install
npm run dev
```

## Run Tests

```bash
cd backend
pytest
```

## Eval & Experiments

Trajecta's eval surface has four pillars (see [docs/testing.md](docs/testing.md)):
the golden set, the deterministic unit suite, RAGAS, and the LLM judge with
Cohen's κ.

### Golden set

`eval/golden.jsonl` — 35 cases built from `data/triage_notes.csv`, schema
`{input, expected_facts, forbidden_facts, tags}`. Eight categories
(allrecipes, amazon, apple, arxiv, booking, github, google_flight,
huggingface). Rebuild with `python scripts/build_golden_jsonl.py`.

### Agent-quality evaluation

```bash
cd backend
OPENAI_API_KEY=sk-... TRAJECTA_AGENT_MODEL=gpt-4o-mini TRAJECTA_VLM_MODEL=gpt-4o-mini \
  python -m app.agent_eval --trace-dir eval/runs/$(date -u +%Y-%m-%dT%H-%M-%SZ)/traces
```

Produces `eval/agent_report.{json,md}` plus per-sample trace JSONs under
the `--trace-dir`. Both `eval/agent_report.*` and `eval/runs/` are
`.gitignore`d; they are reproducible local artefacts.

`agent_eval` retries transient provider failures per sample: 429, rate
limit, timeout, and connection errors retry up to 3 times by default.
Tune with `--max-retries`, `--retry-base-s`, and `--retry-max-s`.

If a formal eval is interrupted, resume with the original trace dump dir:

```bash
TRAJECTA_PROMPT_VERSION=v3_balanced_rubric \
python -m backend.app.agent_eval \
  --trace-dir eval/runs/2026-05-30T03-54-45Z/traces
```

Existing `{run_id}.json` traces are reused and not billed again. The prompt
version must match the trace metadata; mismatches fail fast to avoid mixing
v3/v4/v5 outputs. When `--trace-dir` points at `eval/runs/<stamp>/traces`,
the completed `agent_report.{json,md}` is written back to
`eval/runs/<stamp>/` and mirrored to `eval/agent_report.*`.

The formal Phase 8 v1→v5 prompt comparison uses 31 filtered golden-set
samples with `gpt-5.4-mini-2026-03-17` for both the Eval Agent and VLM.
The best headline prompt is `v3_balanced_rubric`:

| Metric | Value |
| --- | --- |
| Binary verdict accuracy | 80.6 % |
| Failure-verdict recall | 85.7 % |
| Success-verdict recall | 76.5 % |
| Mean tool calls / run | 1.68 |
| Mean wall-clock latency / run | 9.96 s |
| Total cost (31 runs) | $1.022 |
| Coarse-to-fine VLM savings | 91.5 % |

The v5 prompt is a deliberate failure-sensitive trade-off: failure recall
reaches 100.0 % and step localization reaches 78.6 %, but success recall
drops to 41.2 %. Full per-round metrics and deltas are in
[docs/experiment_log.md](docs/experiment_log.md).

### Experiment log

| Round | Prompt | Change | Metric delta | Conclusion |
| --- | --- | --- | --- | --- |
| 1 | `v1_minimal` | Baseline — minimal failure-shape instructions, no rubric. | Baseline binary accuracy 74.2 %; success recall 58.8 %; failure recall 92.9 %. | Strong failure sensitivity, but too many successful runs are marked failed. |
| 2 | `v2_success_rubric` | Add an explicit success-shape rubric. | Binary accuracy +3.2 pp; success recall +29.4 pp; failure recall -28.6 pp. | Success hallucinations drop, but the prompt becomes too conservative on failures. |
| 3 | `v3_balanced_rubric` | Balance success/failure criteria and tighten stop conditions. | Binary accuracy +3.2 pp vs v2; mean tool calls -0.68; latency -1.50 s. | Best headline accuracy at 80.6 % with lower tool use. |
| 4 | `v4_search_strategy_rubric` | Clarify successful-run retrieval vs failure-memory retrieval. | Binary accuracy -6.5 pp; failure-type accuracy rises to 57.1 %. | Retrieval guidance helps the advisory failure-type signal, not the headline metric. |
| 5 | `v5_constraint_verification` | Emphasize constraint evidence and failure verification. | Binary accuracy -6.5 pp; failure recall +14.3 pp to 100.0 %; success recall -23.5 pp. | Best for catching failures, but not the best general prompt. |

The judge columns (`acceptable_rate` by judge and κ_LLM,LLM) are added once
the Phase 8 Gemini/OpenAI judge run completes. See
[docs/experiment_log.md](docs/experiment_log.md) for the full table and
per-round source artefacts and caveats.

For the v5 judge run, Judge A (`gemini-3.1-flash-lite`) accepted 13 / 31
drafts and Judge B (`gpt-5.4-mini-2026-03-17`) accepted 15 / 31. The
dual-judge agreement target is met: κ_LLM,LLM = 0.741 on the full
31-case set.

### RAGAS

```bash
python -m backend.app.ragas_eval --trace-dir eval/runs/{timestamp}/traces --limit 10
```

Reads recorded RAG tool queries and their matching retrieved contexts from the
selected trace dump, then falls back to the SQLite `traces` table only when a
dump is missing. Produces `eval/ragas_report.{json,md}` with no-ground-truth
retrieval-grounded `faithfulness`; it is not an answer-correctness or human
ground-truth evaluation.

The stub-mode fallback remains for offline development but is **not** an
acceptable production artefact under S18 § 2.2 Build 3 — `mode == "real"`
is required, sample count `≥ 10`.

Latest Phase 8 A6 run: `eval/ragas_report.{json,md}` was generated from the
v5 traces with `--limit 10`, `mode=real`, `ground_truth_source=none`, and
`faithfulness=0.4068`.

### LLM judge + Cohen's κ

```bash
# From the repo root.
# Phase 8 target flow: agent_eval writes the report/traces, then runs
# the env-configured Gemini/OpenAI judge post-step.
python -m backend.app.agent_eval \
    --trace-dir eval/runs/{timestamp}/traces \
    --judge

# Standalone judge rerun + κ_LLM,LLM rollup
python -m eval.judge \
    --golden eval/golden.jsonl \
    --report eval/agent_report.json \
    --trace-dir eval/runs/{timestamp}/traces \
    --out eval/judge_report.json
```

The judge scores the single binary dimension `acceptable_eval_case`: is
the generated eval case draft acceptable as a reusable regression case?
Judge A uses a Gemini-compatible provider/model configured by
`TRAJECTA_JUDGE_A_MODEL`; Judge B uses an OpenAI-compatible provider/model
configured by `TRAJECTA_JUDGE_B_MODEL`. Both output `acceptable` or
`unacceptable` plus acceptability assertions over the same resolved case
payload and rubric. The report contains one primary κ row: κ_LLM,LLM.
Preferred N is 31 gradeable cases; when cost-constrained, a deterministic
pre-registered stratified subset is allowed if the report states
`sample_size`, `selection_policy`, and skipped counts. When κ < 0.6, the
report includes disagreement analysis over the split assertions — we do
**not** silently relax the judge contract.

A human second judge is deliberately deferred because reviewer UI, workflow,
and label-management design would expand Phase 8 scope.

See [docs/testing.md](docs/testing.md) § "LLM Judge" for the judge
contract and [docs/failure_analysis.md](docs/failure_analysis.md) for
case studies.

## Planned MCP Connection

MCP is a planned, lower-priority Phase 8 item after the judge agreement
path. The planned server will expose the entire Eval Agent as a composite tool
so external coding agents (Claude Code, Cursor) can diagnose a
browser-agent trajectory via one MCP call.

The design uses the standalone `fastmcp` package
(`pip install fastmcp`); tool registration is decorator-based and JSON
schemas are auto-derived from Python type hints. Once `mcp/server.py`
exists, add to
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "trajecta": {
      "command": "python",
      "args": ["mcp/server.py"],
      "cwd": "<path to Trajecta repo>"
    }
  }
}
```

Demo (Claude Code session):

```text
You: List my Trajecta runs.
Claude Code: <calls trajecta.list_runs(), picks a failed sample>
You: Why did this booking run fail?
Claude Code: <calls trajecta.analyze_run(run_id, intent="analyze_run")>
             <Trajecta runs the LangGraph Eval Agent: digest → suspicious
              step inspection → failure_memory retrieval → propose_eval_case>
             <returns EvalCase draft + AgentTrace, summarises for you>
You: <opens Trajecta UI to validate the draft — MCP cannot mark it validated>
```

The planned MCP surface deliberately excludes `save_validated_eval_case`,
`delete_*`, and `import_dataset`. Validation stays HITL-gated on the
Trajecta-UI side. Full design in [docs/mcp.md](docs/mcp.md); the exclusion
list is also the primary least-privilege artefact in
[docs/security_governance.md](docs/security_governance.md) § Mechanism 7.

## Example Eval Case

```json
{
  "case_id": "ec_run_001_step_3",
  "source_run_id": "run_001",
  "task": "Find a hotel under $200 with free parking.",
  "failure_step": 3,
  "failure_type": "missed_constraint",
  "expected_behavior": "The agent should verify price and free parking before selecting a hotel.",
  "actual_behavior": "The agent selected a hotel without verifying the free parking constraint.",
  "evidence": [
    {
      "claim": "Step 3 selected a hotel result.",
      "source": "step_detail_high",
      "run_id": "run_001",
      "step_index": 3,
      "trace_event_seq": 4,
      "context_id": null
    },
    {
      "claim": "No inspected step verified free parking before selection.",
      "source": "trajectory",
      "run_id": "run_001",
      "step_index": null,
      "trace_event_seq": null,
      "context_id": null
    },
    {
      "claim": "Failure memory fm_missed_constraint_001 describes agents selecting an item before checking a required constraint.",
      "source": "failure_memory",
      "run_id": null,
      "step_index": null,
      "trace_event_seq": 6,
      "context_id": "fm_missed_constraint_001"
    }
  ],
  "regression_rule": "Pass only if the selected hotel satisfies both the price and free parking constraints.",
  "retrieved_context_ids": ["fm_missed_constraint_001"],
  "human_validated": true
}
```

## Roadmap

v1 focuses on local fixtures, deterministic preprocessing, a bounded
tool-calling Eval Agent, ChromaDB retrieval, eval case export, pytest
coverage, and a simple React UI.

**Phase 8** (S18 capstone alignment) prioritizes eval rigor: golden set,
Gemini/OpenAI dual LLM judge with κ_LLM,LLM, real RAGAS, experiment log,
failure analysis, and Security / Governance component framing. Human judge
validation is deferred because reviewer UI, workflow, and label-management
design would expand scope. The MCP composite remains planned but lower
priority than the judge path. See
[docs/phase8_s18_alignment.md](docs/phase8_s18_alignment.md) for the
operating spec and [docs/roadmap.md](docs/roadmap.md) for the full plan.

**v2** may add recorder middleware, expanded MCP support (e.g. a
Reviewer Agent for proposer-critic role split), run comparison, richer
failure memory search UI, OpenTelemetry, and multi-user features.
