# SPEC.md

# Project: Trajecta

## One-liner

Trajecta is an open-source, AI-native Eval Agent for browser-use agent trajectory evaluation.

It imports `allenai/MolmoWeb-HumanSkills` trajectories, replays screenshots and actions in a visual UI, uses a LangGraph-based Eval Agent with tools and ChromaDB-backed RAG to analyze failures, and turns human-validated failures into reusable regression eval cases.

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
- Time-permitting MCP and Skill-style workflow packaging

This is not a browser-use agent.

This is an Eval Agent for browser-use agent trajectories.

## Core User Flow

1. User selects an imported browser-agent trajectory run.
2. UI shows step-by-step screenshots and actions.
3. Backend runs Trajectory Preprocessing on the run and produces a trajectory digest.
4. User clicks `Analyze Run` or `Analyze Step`.
5. The Eval Agent autonomously calls tools: deep-dive on suspicious steps, retrieve similar failures from ChromaDB, and propose an eval case via a terminal tool.
6. UI shows the agent's reasoning trace, retrieved cases, and the proposed eval case draft.
7. User confirms or edits the failure label.
8. System exports `eval_case.json`.
9. Tests and RAGAS eval verify basic quality.

## MVP Priorities

1. Keep the MVP small.
2. Do not build live browser control.
3. Do not build recorder middleware in v1.
4. Focus on tool-using Eval Agent, LangGraph orchestration, ChromaDB RAG, eval case generation, tests, and simple UI.

## MVP Outputs

- Imported sample trajectory runs from `allenai/MolmoWeb-HumanSkills` (≥5 runs).
- Normalized Trajecta JSON backed by Pydantic schemas.
- Screenshot replay UI with validated coordinate overlays.
- Trajectory Preprocessing pipeline producing a per-run trajectory digest. See [docs/preprocessing.md](docs/preprocessing.md).
- LangGraph **tool-calling Eval Agent** that autonomously inspects suspicious steps, retrieves similar failures, and proposes an eval case via a terminal tool.
- ChromaDB-backed failure-memory and eval-case retrieval.
- Per-run agent trace (the `traces` SQLite row in `data/trajecta.db`, accessed via `storage.load_trace` / `storage.save_trace`) consumed by the API, frontend, and RAGAS.
- Human-reviewable eval case draft and export flow with structured evidence references.
- pytest coverage plus RAGAS or fallback retrieval/grounding evaluation.

## Design Decisions

These are the load-bearing decisions for v1. Each is justified by task characteristics, not by stack preference.

1. **The main analysis flow is an agent, not a pipeline.**
   Trajectories vary in length and failure mode. The work of "find the failure" needs dynamic information gathering: skim, hypothesize, zoom in, backtrack, retrieve, decide when to stop. A deterministic DAG cannot express this; a tool-calling agent can. See [docs/eval_agent.md](docs/eval_agent.md).
2. **Trajectory Preprocessing runs the same work on every step.**
   Every step gets the same treatment: one low-detail VLM call plus action parsing, in order, in a `for` loop. The VLM call is a model invocation (so per-step *content* is not bit-identical across runs), but no model chooses which steps to process or in what order. The agent consumes the resulting digest. See [docs/preprocessing.md](docs/preprocessing.md).
3. **Coarse-to-fine VLM.**
   Low-detail (~85 tokens/image) for the digest; high-detail (~1500 tokens/image) only on steps the agent explicitly inspects. The cost ablation (≈80% reduction on a 30-step run) is part of the demo.
4. **`propose_eval_case` is a terminal tool, not free-form output.**
   The agent indicates "I have enough evidence" by calling the tool. Schema is enforced by the tool signature, eliminating a class of JSON-parsing failures.
5. **Tool-call budget bounds cost and latency.**
   Default 8 calls per run. Exceeding the budget terminates the loop with `terminated_by="budget_exceeded"` rather than runaway tool use.
6. **Human-in-the-loop is mandatory before export.**
   The agent proposes; the human validates. No `EvalCase` is exported with `human_validated = false`.

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

## Documentation Map

| File | Purpose |
| --- | --- |
| [docs/product_scope.md](docs/product_scope.md) | Product positioning, v1 scope, non-goals, and core user flow. |
| [docs/architecture.md](docs/architecture.md) | Recommended stack, repository structure, and system boundaries. |
| [docs/contracts.md](docs/contracts.md) | Single source of truth for schemas, tool contracts, API endpoints, RAG collections, and screenshot access. |
| [docs/data_model.md](docs/data_model.md) | Implementation notes for Pydantic schemas defined in `docs/contracts.md`. |
| [docs/dataset_import.md](docs/dataset_import.md) | MolmoWeb-HumanSkills sample import strategy and coordinate validation risk. |
| [docs/preprocessing.md](docs/preprocessing.md) | Trajectory Preprocessing: digest schema, low-detail VLM contract, caching, fallbacks. |
| [docs/eval_agent.md](docs/eval_agent.md) | LangGraph Eval Agent behavior, loop design, observability, and Skill wrapper. |
| [docs/prompt_versioning.md](docs/prompt_versioning.md) | Prompt version registry, traceability, rollback, and failure-memory refresh rules. |
| [docs/rag.md](docs/rag.md) | ChromaDB RAG retrieval strategy. |
| [docs/api.md](docs/api.md) | FastAPI implementation notes for endpoint contracts. |
| [docs/frontend.md](docs/frontend.md) | React UI layout, components, and product copy. |
| [docs/testing.md](docs/testing.md) | pytest, RAGAS or fallback evaluation, and acceptance criteria. |
| [docs/roadmap.md](docs/roadmap.md) | MCP, one-week build plan, README requirements, roadmap, and resume bullets. |

## Authoritative Files

- `AGENTS.md` is the authoritative source for coding-agent development rules.
- `docs/contracts.md` is the authoritative source for shared schemas, tools, API, RAG, and screenshot access.
- Other `docs/*` files explain behavior and implementation strategy without redefining contracts.
- `README.md` is for human-facing project introduction, setup, demo, and roadmap.

## Data and Evidence Rules

- Use Pydantic schemas for all trajectory and eval case structures.
- All agent outputs must be valid JSON.
- Do not invent evidence not present in the trajectory.
- If a screenshot or coordinate is missing, mark it as unavailable.
- If coordinates are invalid, do not draw overlay markers.
- Human validation is required before an eval case is considered final.
