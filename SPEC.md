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
3. User clicks `Analyze Run` or `Analyze Step`.
4. Trajecta Eval Agent calls tools to inspect the run, retrieve similar failure cases from ChromaDB, and analyze evidence.
5. Eval Agent generates structured failure analysis and an eval case draft.
6. User confirms or edits the failure label.
7. System exports `eval_case.json`.
8. Tests and RAGAS eval verify basic quality.

## MVP Priorities

1. Keep the MVP small.
2. Do not build live browser control.
3. Do not build recorder middleware in v1.
4. Focus on tool-using Eval Agent, LangGraph orchestration, ChromaDB RAG, eval case generation, tests, and simple UI.

## MVP Outputs

- Imported sample trajectory runs from `allenai/MolmoWeb-HumanSkills`.
- Normalized Trajecta JSON backed by Pydantic schemas.
- Screenshot replay UI with validated coordinate overlays.
- Tool-using LangGraph Eval Agent that returns JSON only.
- ChromaDB-backed failure-memory retrieval.
- Human-reviewable eval case draft and export flow.
- pytest coverage plus RAGAS or fallback retrieval evaluation.

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
| [docs/data_model.md](docs/data_model.md) | Pydantic schemas for trajectories, coordinate validation, failure memory, and eval cases. |
| [docs/dataset_import.md](docs/dataset_import.md) | MolmoWeb-HumanSkills sample import strategy and coordinate validation risk. |
| [docs/eval_agent.md](docs/eval_agent.md) | LangGraph Eval Agent tools, state, nodes, behavior, output schema, and Skill wrapper. |
| [docs/rag.md](docs/rag.md) | ChromaDB RAG collections and retrieval flow. |
| [docs/api.md](docs/api.md) | FastAPI endpoint surface. |
| [docs/frontend.md](docs/frontend.md) | React UI layout, components, and product copy. |
| [docs/testing.md](docs/testing.md) | pytest, RAGAS or fallback evaluation, and acceptance criteria. |
| [docs/roadmap.md](docs/roadmap.md) | MCP, one-week build plan, README requirements, roadmap, and resume bullets. |

## Authoritative Files

- `AGENTS.md` is the authoritative source for coding-agent development rules.
- `docs/*` are the authoritative detailed product and implementation specifications.
- `README.md` is for human-facing project introduction, setup, demo, and roadmap.

## Data and Evidence Rules

- Use Pydantic schemas for all trajectory and eval case structures.
- All agent outputs must be valid JSON.
- Do not invent evidence not present in the trajectory.
- If a screenshot or coordinate is missing, mark it as unavailable.
- If coordinates are invalid, do not draw overlay markers.
- Human validation is required before an eval case is considered final.
