# Roadmap

## MCP

MCP is optional for v1.

If time permits, create minimal MCP server in `mcp/server.py` exposing:

```text
search_failure_memory(query, top_k=3)
create_eval_case_draft(run_id, failure_step, failure_type)
```

`search_failure_memory` wraps the same failure-memory retrieval path used by the
Eval Agent's internal `search_similar_cases` helper. It searches the
`failure_memory` collection.

`create_eval_case_draft` is a high-level MCP wrapper that runs the Eval Agent
flow and returns an `EvalCase`-shaped draft. It should not be confused with the
LangGraph `generate_eval_case` node or the deterministic `assemble_eval_case`
helper.

If a later MCP tool named `search_eval_cases` is added, it should explicitly
search the `eval_cases` collection, not failure memory.

Do not spend more than half a day on MCP.

## One-Week Build Plan

### Stage 1

- Create repo
- Add schemas
- Add at least 5 small MolmoWeb-HumanSkills sample fixtures
- Add failure memory cases
- Add basic tests

### Stage 2

- Implement dataset importer
- Implement coordinate validator
- Implement backend storage and tools

### Stage 3

- Implement ChromaDB RAG
- Implement LangGraph Eval Agent
- Add eval case generator

### Stage 4

- Add RAGAS eval script
- Finish pytest coverage

### Stage 5

- Build React UI
- Run list
- Step timeline
- Screenshot viewer
- Step details

### Stage 6

- Add Eval Agent panel
- Wire Analyze Run / Analyze Step
- Export eval case
- Add SKILL.md if time permits
- Optional minimal MCP server

### Stage 7

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

- Built Trajecta, an AI-native Eval Agent for browser-agent trajectory evaluation that converts raw trajectories into human-reviewable regression eval cases.
- Implemented a LangGraph-based tool-calling workflow for trajectory inspection, screenshot analysis, similar failure retrieval, and structured eval-case generation.
- Built ChromaDB-backed failure-memory RAG and evaluated retrieval-grounded analysis using lightweight RAGAS metrics.
- Designed a React replay UI for screenshot-based trajectory inspection, step-level failure labeling, coordinate validation, and eval case export.
- Added pytest coverage for schemas, MolmoWeb import, coordinate validation, tools, ChromaDB retrieval, and eval-case generation.
