# EvalTrace Lite

EvalTrace Lite is an AI-native eval agent that turns browser-agent trajectories into human-reviewable regression eval cases.

## What this MVP includes

- Dataset-based trajectory importer from local fixtures (`data/runs/*/trajectory.json`)
- Unified trajectory schema (Pydantic models)
- Tool-using Eval Agent (step lookup, analysis, memory retrieval, eval-case generation)
- Failure-memory RAG over JSONL data
- LLM/VLM-style structured analysis interface (implemented as deterministic MVP logic)
- Human-reviewable `eval_case.json` generation
- FastAPI backend
- React + TypeScript UI for run/step replay metadata and analysis actions
- Pytest suite
- Lightweight RAGAS-like evaluation script
- Skill-style packaging and minimal MCP tool server

## Repository layout

```text
backend/            FastAPI app, schemas, tools, eval agent, tests
frontend/           Vite React TypeScript UI
data/               Sample runs, failure memory, generated eval cases
skills/             Skill packaging docs
mcp/                Minimal MCP-like tool server
```

## Quickstart

### Backend

```bash
cd backend
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

The UI expects backend API at `http://127.0.0.1:8000` by default. Override with:

```bash
VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

## API endpoints

- `GET /health`
- `GET /runs`
- `GET /runs/{run_id}`
- `POST /analyze/{run_id}/{step_id}`

## Tests

```bash
cd backend
python -m pytest -q
```

## Lightweight RAGAS evaluation

```bash
cd backend
python -m app.ragas_eval --run-id run_001 --step-id step_002
```

## Demo flow

1. Open UI and select a trajectory run.
2. Inspect step timeline and metadata.
3. Click **Analyze Step** or **Analyze Run**.
4. Review generated analysis and eval-case draft.
5. Find exported eval case under `data/eval_cases/generated/`.
