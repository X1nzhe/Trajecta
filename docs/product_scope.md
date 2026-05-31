# Product Scope

## Project Positioning

Trajecta is an open-source, AI-native Eval Agent for browser-use agent trajectory evaluation.

It imports `allenai/MolmoWeb-HumanSkills` trajectories, replays screenshots and actions in a visual UI, uses a LangGraph-based Eval Agent with tools and ChromaDB-backed RAG to analyze failures, and turns human-validated failures into reusable regression eval cases.

This is not a browser-use agent.

This is an Eval Agent for browser-use agent trajectories.

## Primary Goal

Build a one-week MVP for an AI Engineer / AI Infra Engineer portfolio project.

The project must clearly demonstrate:

- Agent workflow
- Tool/function calling
- RAG over failure memory
- LLM/VLM-assisted trajectory analysis
- Evaluation case generation
- Testing and evaluation
- RAGAS-based retrieval/grounding evaluation
- MCP composite access verified through MCP Inspector

## Core User Flow

1. User selects an imported browser-agent trajectory run.
2. UI shows step-by-step screenshots and actions.
3. Backend runs Trajectory Preprocessing on the run and produces a trajectory digest.
4. User clicks `Analyze Run` or `Analyze Step`.
5. The Eval Agent autonomously decides which steps to deep-dive (via `get_step_detail`), pulls a similar successful run for the same task and diffs against it (via `find_similar_successful_run` + `get_run`), retrieves similar failures from ChromaDB (via `search_failure_memory` / `search_eval_cases`), and terminates by calling `propose_eval_case`.
6. UI renders the agent's tool-call trace, retrieved cases, and the proposed eval case draft as a chat-style timeline.
7. User may ask follow-up questions in the chat input ("why did you flag step 5?", "find similar failures"); the agent resumes the same trace with a smaller per-turn budget and may revise the draft.
8. User confirms or edits the failure label and marks the draft validated.
9. System exports `eval_case.json`.
10. Tests and RAGAS eval verify basic quality.

## Must Have in v1

- Import or load a checked-in sample subset derived from Hugging Face dataset: `allenai/MolmoWeb-HumanSkills` (≥5 runs, including at least one `status=success` run per fixture task category so replay-and-diff is reachable)
- Normalize raw trajectory data into the Trajecta JSON schema
- Visual trajectory replay UI
- Screenshot viewer with coordinate overlay rendered only when validated
- Trajectory Preprocessing pipeline producing a per-run trajectory digest (see [docs/preprocessing.md](preprocessing.md))
- LangGraph **tool-calling Eval Agent** with `get_run`, `get_step_detail`, `find_similar_successful_run`, `search_failure_memory`, `search_eval_cases`, and a terminal `propose_eval_case` tool
- Replay-and-diff: agent retrieves a similar successful run for the same task and reasons over step-level divergence
- Multi-turn follow-up: after the initial analyze, the user may ask follow-up questions via `POST /api/runs/{run_id}/followup`. The agent resumes the same trace, may revise the eval case draft, and is bounded by a per-turn tool-call budget
- Tool-call budget enforcement (default 8 per turn, applied independently to the initial analyze and to each follow-up) bounding cost and latency
- ChromaDB-backed RAG over failure memories, eval cases, and successful runs
- Multi-resolution VLM (low-detail for preprocessing, high-detail on demand)
- Per-run agent trace persisted as the `traces` row keyed by `run_id` in `data/trajecta.db`
- Human-reviewable eval case draft and export flow with structured evidence references
- Basic pytest test suite
- Minimal no-ground-truth RAGAS faithfulness script over recorded RAG tool queries; stub fallback is for offline development only
- README with architecture and demo instructions
- FastAPI backend
- Pydantic schemas
- React + TypeScript + Vite + Tailwind frontend
- Local file storage for screenshots
- Local ChromaDB persistence
- `AGENTS.md` at repo root


## Not In Current V1 Closeout

- `skills/create-eval-case/SKILL.md`
- Reviewer UI / human second judge workflow
- Spotlighting security benchmark
- v2 backlog work

## Nice to Have

- MCP server (shipped in Phase 8 B1 and verified with MCP Inspector)
- Run comparison
- Failure memory search UI
- Coordinate validation report for MolmoWeb samples

## Must Not Have in v1

- No live browser control
- No CDP / Playwright recorder middleware
- No OS-level computer-use support
- No video replay
- No OpenTelemetry integration
- No multi-user auth
- No SaaS features
- No large full-dataset download requirement
- No automatic root-cause claim without human review

Recorder middleware is v2.
