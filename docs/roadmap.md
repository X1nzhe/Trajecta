# Roadmap

## MCP

MCP is optional for v1.

If time permits, create a minimal MCP server in `mcp/server.py` that re-exposes
a subset of the Eval Agent's contracted tools and an `analyze_run` wrapper.
Core tool signatures live in [docs/contracts.md](contracts.md#agent-tool-contracts).

Each MCP tool delegates to the same backend function used by the in-process Eval Agent. Do not duplicate logic; the MCP layer is a thin transport adapter.

Do not spend more than half a day on MCP.

## One-Week Build Plan

### Phase 1

- Create repo
- Add schemas
- Add at least 5 small MolmoWeb-HumanSkills sample fixtures
- Commit `data/raw/molmoweb_humanskills_sample/run_status_overlay.json` covering those fixtures (≥1 `success` entry per task category)
- Commit 1 example `data/eval_cases/validated/{case_id}.json` so `GET /api/eval-cases` is non-empty on a fresh clone
- Commit 1 example `data/runs/{run_id}/last_trace.json` so frontend dev has a fixture before the agent loop is wired up
- Add failure memory cases
- Add basic tests

### Phase 2

- Implement dataset importer
- Implement coordinate validator
- Implement backend storage and tools

### Phase 3

- Implement Trajectory Preprocessing (`preprocess.py`) per [docs/preprocessing.md](preprocessing.md): build the `trajectory_digest` with low-detail VLM hints, parsed actions, and coordinate validation
- Implement ChromaDB RAG (`failure_memory` + `eval_cases` + `successful_runs` collections)
- Implement the LangGraph tool-calling Eval Agent with `get_run`, `get_step_detail`, `find_similar_successful_run`, `search_failure_memory`, `search_eval_cases`, and the terminal `propose_eval_case` tool
- Convert the agent loop's final `messages` into an `AgentTrace` and persist to `data/runs/{run_id}/last_trace.json` (overwritten each analyze)

### Phase 4

- Add RAGAS eval script
- Finish pytest coverage

### Phase 5

- Build React UI
- Run list
- Step timeline
- Screenshot viewer
- Step details (Action / Observation / Coordinate Validation / Metadata tabs)

### Phase 6

- Add Eval Agent panel as a chat-style timeline (renders trace events grouped by turn)
- Wire Analyze Run / Analyze Step as the only fresh-trace entrypoints
- Implement `POST /api/runs/{run_id}/followup` endpoint and the chat input + prompt chips that drive it (~+1-1.5 days vs. button-only design)
- Termination badge, View Draft / Mark validated / Export eval case flow
- Visual-only thumbs feedback (not wired)
- Footer dataset/run summary
- Add SKILL.md if time permits
- Optional minimal MCP server

### Phase 7

- Polish demo
- Add screenshots / GIF
- Run tests
- Produce `ragas_report.md`
- Prepare README and resume bullets

## README Requirements

README must include:

- What the project is
- Why it exists
- Architecture diagram
- Demo flow
- Setup
- LLM/VLM configuration and environment variables
- Run backend
- Run frontend
- Run tests
- Run RAGAS eval
- Example eval case
- Roadmap

README tagline:

```text
Trajecta turns raw browser-agent trajectories into human-validated regression eval cases.
```

## v2 and Backlog

- Recorder middleware
- MCP expansion
- Expanded Skill-style workflow packaging
- Run comparison
- Failure memory search UI
- Coordinate validation report for MolmoWeb samples
- OpenTelemetry integration
- Multi-user auth
- SaaS features

## Resume Bullets

Use after completion:

- Built **Trajecta**, an AI-native Eval Agent for browser-agent trajectory evaluation that converts raw trajectories into human-reviewable regression eval cases.
- Designed and implemented a **LangGraph tool-calling agent** with multi-turn follow-up: the agent autonomously decides which trajectory steps to deep-dive, when to retrieve failure memory, and when to terminate via a typed `propose_eval_case` tool — and users can ask follow-up questions that resume the same trace under a smaller per-turn budget, with the agent free to revise the draft.
- Reduced visual-token cost ~80% via a **coarse-to-fine VLM strategy**: Trajectory Preprocessing calls a low-detail VLM (~85 tokens/image) on every step to build a digest, while high-detail VLM is invoked on demand by the agent only for steps it flags as suspicious.
- Built **ChromaDB-backed RAG** over failure memory and prior eval cases, with agent-authored queries and traceable `retrieved_context_ids` linking each generated case back to its supporting evidence.
- Produced a structured per-run `AgentTrace` rendered in the UI and consumed by RAGAS.
- Evaluated retrieval-grounded analysis with **RAGAS faithfulness and context-precision** metrics; documented an ablation comparing all-steps-high-detail vs agent-driven on-demand inspection cost.
- Built a **React replay UI** for screenshot-based trajectory inspection, coordinate validation, agent reasoning visualization, and eval case export.
- Added pytest coverage for schemas, MolmoWeb import, coordinate validation, preprocessing, tools, ChromaDB retrieval, and the agent loop.
