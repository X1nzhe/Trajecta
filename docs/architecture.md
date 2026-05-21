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
- Structured JSON outputs enforced by tool signatures (no free-form JSON parsing)
- Function tools as the agent's only interface to the run, RAG, and final output
- Multi-resolution VLM: low-detail (~85 tokens/image) for preprocessing, high-detail (~1500 tokens/image) on demand
- Prompt layout keeps the stable prefix (system prompt + trajectory digest) before dynamic tool-calling turns, so a caching-capable provider benefits transparently. v1 does not depend on cache discounts and does not wire provider-specific cache controls.
- Embeddings for failure-memory and eval-case RAG

Configuration:

- Load configuration from environment variables. Local development may use
  `backend/.env`, which must not be committed.
- `OPENAI_API_KEY`: required only when real LLM/VLM calls are enabled
- `OPENAI_BASE_URL`: optional override for OpenAI-compatible providers
- `TRAJECTA_LLM_MODEL`: text model for trajectory analysis
- `TRAJECTA_VLM_MODEL`: vision model for screenshot summaries
- `TRAJECTA_EMBEDDING_MODEL`: embedding model for ChromaDB indexing
- `TRAJECTA_CHROMA_DIR`: ChromaDB persistence directory; defaults to `data/chroma/`

Changing `TRAJECTA_EMBEDDING_MODEL` requires clearing and rebuilding persisted
ChromaDB collections or using model-specific collection names.

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
      ids.py
      llm.py
      dataset_importer.py
      coordinate_validator.py
      preprocess.py
      tools.py
      eval_agent_graph.py
      rag.py
      ragas_eval.py
    tests/
      test_schema.py
      test_importer.py
      test_preprocess.py
      test_tools.py
      test_rag.py
      test_eval_agent.py
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
        run_status_overlay.json
    runs/
      {run_id}/
        trajectory.json
        digest.json
        last_trace.json
        screenshots/
    failure_memory/
      cases.jsonl
    eval_cases/
      validated/
        {case_id}.json
    chroma/
  skills/
    create-eval-case/
      SKILL.md
  mcp/
    server.py
  eval/
    ragas_report.json
    ragas_report.md
```

The repo should include at least 5 fixture runs under `data/runs/` for the MVP,
but the structure above is shown as a template because additional run folders are
runtime data.

Module responsibilities:

- `storage.py`: load and save trajectory runs, digests, traces, and eval cases from local disk; provide existence checks used by `ids.make_eval_case_id`.
- `ids.py`: generate stable eval-case IDs and check collisions through storage.
- `llm.py`: centralize LLM/VLM client creation, provider configuration, and deterministic offline mocks.
