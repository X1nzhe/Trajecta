# Architecture

## Recommended Tech Stack

### Backend

- Python
- FastAPI
- Pydantic
- LangGraph
- ChromaDB
- pytest
- RAGAS

### Frontend

- React
- TypeScript
- Vite
- Tailwind

### AI

- OpenAI-compatible LLM/VLM API
- Structured JSON outputs
- Function tools
- Embeddings for failure-memory RAG

Configuration:

- Load configuration from environment variables. Local development may use
  `backend/.env`, which must not be committed.
- `OPENAI_API_KEY`: required only when real LLM/VLM calls are enabled
- `OPENAI_BASE_URL`: optional override for OpenAI-compatible providers
- `TRAJECTA_LLM_MODEL`: text model for trajectory analysis
- `TRAJECTA_VLM_MODEL`: vision model for screenshot summaries
- `TRAJECTA_EMBEDDING_MODEL`: embedding model for ChromaDB indexing

Tests and local fixtures must not require network calls. If API credentials are
missing, use deterministic mocked LLM/VLM summaries and agent outputs.

### Data

- Source dataset: `allenai/MolmoWeb-HumanSkills`
- Use only a small sampled subset in v1
- Do not require full dataset download

## System Boundaries

Trajecta is an Eval Agent for browser-use agent trajectories. It imports existing trajectory data, displays the run, analyzes failures, retrieves similar failure memory, and drafts regression eval cases.

Trajecta does not control a live browser in v1. It does not include CDP, Playwright recorder middleware, OS-level computer-use support, video replay, multi-user auth, OpenTelemetry integration, SaaS features, or automatic root-cause claims without human review.

## Repository Structure

```text
trajecta/
  README.md
  SPEC.md
  AGENTS.md
  backend/
    app/
      main.py
      schemas.py
      storage.py
      dataset_importer.py
      coordinate_validator.py
      tools.py
      eval_agent_graph.py
      rag.py
      ragas_eval.py
      eval_case_generator.py
    tests/
      test_schema.py
      test_importer.py
      test_tools.py
      test_rag.py
      test_eval_case.py
      test_coordinates.py
      test_api.py
    requirements.txt
  frontend/
    package.json
    src/
      App.tsx
      components/
        RunList.tsx
        StepTimeline.tsx
        ScreenshotViewer.tsx
        StepDetailPanel.tsx
        EvalAgentPanel.tsx
        EvalCaseDraft.tsx
  data/
    raw/
      molmoweb_humanskills_sample/
    runs/
      run_001/
        trajectory.json
        screenshots/
      run_002/
        trajectory.json
        screenshots/
      run_003/
        trajectory.json
        screenshots/
      run_004/
        trajectory.json
        screenshots/
      run_005/
        trajectory.json
        screenshots/
    failure_memory/
      cases.jsonl
    eval_cases/
      generated/
  skills/
    create-eval-case/
      SKILL.md
  mcp/
    server.py
  eval/
    ragas_report.json
    ragas_report.md
```
