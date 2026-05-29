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

# Optional: ChromaDB embedding model. Falls back to chromadb's default
# sentence-transformers if unset. Changing this requires clearing
# data/chroma/ to rebuild the index — collections are not migrated.
TRAJECTA_EMBEDDING_MODEL=text-embedding-3-small
```

Prompt updates are versioned directories under `prompts/eval_agent/` and
`prompts/vlm_high_detail/`. Create a new directory for each prompt change and
roll back by setting the corresponding environment variable to a previous
version. See
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

The v5 baseline (prompt `v5_constraint_verification`, 31 samples,
`gpt-5.4-mini-2026-03-17`) lands at:

| Metric | Value |
| --- | --- |
| Binary verdict accuracy | 74.2 % |
| Failure-verdict recall | 100.0 % |
| Success-verdict recall | 52.9 % |
| Mean tool calls / run | 1.87 |
| Mean wall-clock latency / run | 27.92 s |
| Total cost (31 runs) | $0.9987 |
| Coarse-to-fine VLM savings | 92.0 % |

Numbers cited above come from the local `eval/agent_report.md` — regenerate
with the command above. The repo intentionally does not commit the report so
README claims stay in sync with whatever the user can produce in a fresh run.

### Experiment log

| Round | Prompt | Change | Metric delta | Conclusion |
| --- | --- | --- | --- | --- |
| 1 | `v1_minimal` | Baseline — minimal failure-shape instructions, no rubric. | — | Baseline binary accuracy + recall split established. |
| 2 | `v2_success_rubric` | Add an explicit success-shape rubric so the agent stops hallucinating failure when the run succeeded. | Success-verdict recall ↑. Failure-verdict recall ≈ flat. | Honest success calls became measurable. |
| 3 | `v3_balanced_rubric` | Symmetric success / failure rubric; clearer guidance on when to stop calling `get_step_detail`. | Mean tool calls ↓. Binary accuracy flat. | Cost down without quality loss. |
| 4 | `v4_search_strategy_rubric` | Prompt teaches when to call `find_similar_successful_run` vs `search_failure_memory`. | Failure_type advisory metric ↑; binary accuracy flat. | Targeted retrieval helps the advisory signal, not the headline. |
| 5 | `v5_constraint_verification` | Constraint-evidence rubric for the high-detail VLM; agent required to surface constraint satisfaction in evidence. | Binary accuracy reaches 74.2 % (vs majority 54.8 %); failure-verdict recall 100 %; success-verdict recall 52.9 % | Constraint-grounded evidence is the highest-leverage prompt change in the v1→v5 sequence. |

The judge column (`acceptable_rate` per A4) is added once the Phase 8 LLM
judge run completes. See [docs/experiment_log.md](docs/experiment_log.md)
for the full table and per-round failure-mode breakdowns once that doc is
populated.

### RAGAS

```bash
cd backend
python -m app.ragas_eval
```

Reads from the SQLite `traces` table when present and from the most recent
`eval/runs/{ts}/traces/` dir otherwise. Produces `eval/ragas_report.{json,md}`
with `faithfulness` (primary) and `context_precision` (secondary).

The stub-mode fallback remains for offline development but is **not** an
acceptable production artefact under S18 § 2.2 Build 3 — `mode == "real"`
is required, `n ≥ 10`.

### LLM judge + Cohen's κ

```bash
# 1. LLM judge runs, one per model
python -m eval.judge \
    --golden eval/golden.jsonl \
    --report eval/agent_report.json \
    --trace-dir eval/runs/{timestamp}/traces \
    --judge-model claude-opus-4-1 \
    --out eval/judge_report.json

# 2. Human label collection (CLI side-by-side viewer)
python -m eval.judge --human-label-mode \
    --golden eval/golden.jsonl \
    --report eval/agent_report.json \
    --trace-dir eval/runs/{timestamp}/traces \
    --out data/human_judge_labels.jsonl

# 3. κ rollup
python -m eval.judge --rollup --out eval/judge_report.md
```

The judge scores the single binary dimension `acceptable_eval_case` via a
six-clause rubric. Reports two κ rows: κ_LLM,LLM (Claude vs GPT) and
κ_LLM,human (best-LLM vs human-labelled subset). When κ < 0.6, the report
includes a disagreement analysis listing the split cases and the rubric
clauses each annotator failed — we do **not** silently relax the rubric.

See [docs/testing.md](docs/testing.md) § "LLM Judge" for the rubric and
[docs/failure_analysis.md](docs/failure_analysis.md) for case studies.

## Connect Trajecta to Claude Code via MCP

Trajecta ships an MCP server that exposes the entire Eval Agent as a
composite tool. External coding agents (Claude Code, Cursor) diagnose a
browser-agent trajectory via one MCP call.

The server is built on the standalone `fastmcp` package
(`pip install fastmcp`); tool registration is decorator-based and JSON
schemas are auto-derived from Python type hints. Add to
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

The MCP surface deliberately excludes `save_validated_eval_case`,
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

**Phase 8** (S18 capstone alignment) ships the eval rigor and the MCP
composite — golden set, LLM judge with κ, real RAGAS, experiment log,
failure analysis, `mcp/server.py`, and the Security / Governance
component framing. See
[docs/phase8_s18_alignment.md](docs/phase8_s18_alignment.md) for the
operating spec and [docs/roadmap.md](docs/roadmap.md) for the full plan.

**v2** may add recorder middleware, expanded MCP support (e.g. a
Reviewer Agent for proposer-critic role split), run comparison, richer
failure memory search UI, OpenTelemetry, and multi-user features.
