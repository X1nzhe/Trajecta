# AGENTS.md

## Purpose
This file is the root operating manual for coding agents working in Trajecta.

Use it for agent-specific workflow, navigation, and guardrails. Keep long product or implementation details in `PROJECT.md` and `docs/*`, not here.

## Project
Trajecta is an AI-native Eval Agent for browser-agent trajectory evaluation.

It imports existing browser-agent trajectories, replays screenshots/actions, analyzes failures with a tool-using Eval Agent, retrieves similar failure memory with ChromaDB RAG, and turns human-validated failures into reusable regression eval cases.

This is not a browser-use agent. It is an Eval Agent for browser-use agent trajectories.

## Navigation
Start every non-trivial task by reading the smallest relevant set of docs.

- `PROJECT.md`: project entry point, MVP priorities, non-goals, and document map.
- `docs/product_scope.md`: product positioning, v1 scope, v2 boundaries, and core user flow.
- `docs/architecture.md`: recommended stack, repository layout, and system boundaries.
- `docs/contracts.md`: single source of truth for schemas, tool contracts, API endpoints, RAG collections, and screenshot access.
- `docs/data_model.md`: implementation notes for Pydantic schemas defined in `docs/contracts.md`.
- `docs/dataset_import.md`: MolmoWeb-HumanSkills import strategy and coordinate validation rules.
- `docs/preprocessing.md`: Trajectory Preprocessing — digest schema, low-detail VLM contract, caching, and fallbacks.
- `docs/eval_agent.md`: LangGraph Eval Agent workflow, tools, state, output schema, observability, and Skill wrapper.
- `docs/prompt_versioning.md`: prompt version registry, traceability, rollback, and failure-memory refresh rules.
- `docs/rag.md`: ChromaDB collections, embedding text, and retrieval flow.
- `docs/api.md`: FastAPI endpoint surface.
- `docs/frontend.md`: React UI layout, components, and UI copy.
- `docs/testing.md`: pytest coverage, RAGAS or fallback eval, and acceptance criteria.
- `docs/roadmap.md`: optional MCP, one-week build plan, README requirements, backlog, and resume bullets.

`README.md` is human-facing project documentation. Do not treat it as the implementation source of truth when `PROJECT.md` or `docs/*` contains a more specific rule.

If a future subdirectory contains its own `AGENTS.md`, follow that nearest file for work under that subtree while preserving non-conflicting root rules from this file.

## Priorities
1. Keep the MVP small.
2. Do not build live browser control.
3. Do not build recorder middleware in v1.
4. Focus on the tool-calling Eval Agent (LangGraph), Trajectory Preprocessing, coarse-to-fine VLM, ChromaDB RAG, agent tracing, eval case generation, tests, and a simple UI.

## Work Routine
1. Read the relevant docs before editing.
2. Inspect existing code and data shape before inventing new structure.
3. Keep changes scoped to the requested behavior and MVP boundaries.
4. Update docs when a change alters schemas, APIs, workflow, commands, or acceptance criteria.
5. Prefer simple local fixtures and deterministic tests over full dataset or network-dependent flows.
6. Report any command you could not run and why.

## Persistence
- Runs, steps, screenshots (BLOB), digests, traces, eval cases, and the failure-memory mirror live in `data/trajecta.db` (SQLite, single file). Schema in `backend/app/models.py`, Alembic migrations in `backend/alembic/versions/`.
- ChromaDB persists separately under `data/chroma/`. Do not collapse the two stores.
- Always reach the DB through `backend/app/storage.py`. No raw SQL or `Session()` construction elsewhere.

## LLM / VLM Configuration
The backend has two model-selection environment variables that gate the real-vs-mock split. Both default to deterministic mocks when unset, so tests + cold-start demos run without network. See [README.md](README.md) "Configuration" for the full env-var table.
- `OPENAI_API_KEY` + `TRAJECTA_AGENT_MODEL` → tool-calling Eval Agent uses `ChatOpenAI(...).bind_tools([...])`. Without both, `OfflineAgentMock` runs a fixed 5-stage script (`get_run` → `get_step_detail` → `find_similar_successful_run` → `search_failure_memory` → `propose_eval_case`).
- `OPENAI_API_KEY` + `TRAJECTA_VLM_MODEL` → Trajectory Preprocessing + `get_step_detail` use `RealVLMClient` against the OpenAI Chat Completions API with `image_url` content. Without both, `MockVLMClient` returns deterministic hash-derived summaries.
- `TRAJECTA_PROMPT_VERSION` selects a committed prompt bundle under `prompts/eval_agent/` and defaults to `v1_minimal`. New traces and eval reports record `prompt_version` and `prompt_sha256`.
- `TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION` selects a committed high-detail VLM prompt under `prompts/vlm_high_detail/` and defaults to `v1_task_context`. High-detail `get_step_detail` results and eval reports record version + hash.
- The default pytest suite covers the mock paths. The real-LLM agent path has one opt-in smoke test at `backend/tests/test_real_llm_integration.py`; it skips unless `OPENAI_API_KEY` + `TRAJECTA_AGENT_MODEL` are set.

## Commands
Backend:
- cd backend
- pip install -r requirements.txt
- uvicorn app.main:app --reload  # lifespan calls Base.metadata.create_all; no Alembic step required for dev

### Alembic
Alembic is committed (`backend/alembic/`) for future schema evolution, but it is **not** the dev bootstrap path — the FastAPI lifespan runs `create_all` and that is what populates a fresh `data/trajecta.db`. Rules:
- Do **not** run `alembic upgrade head` against a DB the app has already created — it will fail because the tables exist but there is no `alembic_version` row to skip them.
- To use Alembic explicitly: delete `data/trajecta.db` first and run `alembic upgrade head` before starting the app, OR run `alembic stamp head` against the existing DB to mark it as already at head.
- `models.NAMING_CONVENTION` ensures `create_all` and Alembic produce byte-identical index / FK / PK names — do not rename the convention without rewriting `0001_initial_schema.py`.

Tests:
- cd backend
- pytest

Frontend:
- cd frontend
- npm install
- npm run dev

## Coding Rules
- Use the Pydantic schemas defined in `docs/contracts.md` for all trajectory and eval case structures.
- The Eval Agent is a LangGraph tool-calling agent. It must reach trajectory data, RAG, and final output only through declared tools.
- Per-step preprocessing has fixed control flow: a `for` loop calls a low-detail VLM on every step and parses every action. It still invokes the model, but *which* step gets processed and *in what order* is not a model decision. This is not part of the agent.
- High-detail VLM inspection is on demand only — via the `get_step_detail` tool.
- The agent terminates by calling the `propose_eval_case` terminal tool. Free-form JSON output is not used.
- Tool-call budget (default 8) must be enforced; exceeding it terminates the loop with `terminated_by="budget_exceeded"`.
- All agent outputs must be valid JSON and validate against the `EvalCase` schema.
- Do not invent evidence not present in the trajectory.
- If a screenshot or coordinate is missing, mark it as unavailable.
- If coordinates are invalid, do not draw overlay markers.
- Human validation is required before an eval case is considered final.

## Documentation Rules
- Keep `AGENTS.md` concise and operational; move durable product details to `PROJECT.md` or `docs/*`.
- Do not duplicate large sections from docs into this file.
- When adding Claude Code support, prefer a `CLAUDE.md` that imports `AGENTS.md` with `@AGENTS.md` rather than copying these rules.
- Keep all agent-facing instructions imperative, specific, and testable.

## Validation
- For backend changes, run `cd backend && pytest` when dependencies are available.
- For frontend changes, run `cd frontend && npm run dev` for local smoke testing when the app exists.
- For docs-only changes, verify links and headings with `rg` and check file sizes with `wc -l`.
