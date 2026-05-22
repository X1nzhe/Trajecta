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

Local configuration is read from environment variables. You may use `backend/.env`; do not commit secrets.

```text
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
TRAJECTA_LLM_MODEL=...
TRAJECTA_VLM_MODEL=...
TRAJECTA_EMBEDDING_MODEL=...
```

Tests must run without network access. When credentials are missing, preprocessing and agent tests use deterministic mocks.

Changing `TRAJECTA_EMBEDDING_MODEL` requires clearing and rebuilding persisted ChromaDB collections or using model-specific collection names.

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

## RAGAS Eval

```bash
cd backend
python -m app.ragas_eval
```

Expected outputs:

```text
eval/ragas_report.json
eval/ragas_report.md
```

If RAGAS setup is too slow for a local run, use the documented fallback script while preserving the same output paths.

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

v1 focuses on local fixtures, deterministic preprocessing, a bounded tool-calling Eval Agent, ChromaDB retrieval, eval case export, pytest coverage, and a simple React UI.

v2 may add recorder middleware, expanded MCP support, run comparison, richer failure memory search UI, OpenTelemetry, and multi-user features.
