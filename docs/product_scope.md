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
- Time-permitting MCP and Skill-style workflow packaging

## Core User Flow

1. User selects an imported browser-agent trajectory run.
2. UI shows step-by-step screenshots and actions.
3. User clicks `Analyze Run` or `Analyze Step`.
4. Trajecta Eval Agent calls tools to inspect the run, retrieve similar failure cases from ChromaDB, and analyze evidence.
5. Eval Agent generates structured failure analysis and an eval case draft.
6. User confirms or edits the failure label.
7. System exports `eval_case.json`.
8. Tests and RAGAS eval verify basic quality.

## Must Have in v1

- Import or load a checked-in sample subset derived from Hugging Face dataset: `allenai/MolmoWeb-HumanSkills`
- Normalize raw trajectory data into Trajecta JSON schema
- Visual trajectory replay UI
- Screenshot viewer with optional coordinate overlay
- Tool-using Eval Agent
- LangGraph workflow for Eval Agent orchestration
- ChromaDB-backed RAG over failure memories and eval cases
- LLM/VLM-assisted failure analysis
- Human-reviewable eval case generation
- Basic pytest test suite
- Minimal RAGAS evaluation script; fallback allowed if RAGAS setup is too slow
- README with architecture and demo instructions
- FastAPI backend
- Pydantic schemas
- React + TypeScript + Vite + Tailwind frontend
- Local file storage for screenshots
- Local ChromaDB persistence
- `AGENTS.md`


## Should Have

- One `skills/create-eval-case/SKILL.md`

## Nice to Have

- Minimal MCP server exposing 1-2 tools
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
