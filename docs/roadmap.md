# Roadmap

## MCP

MCP is optional for v1.

If time permits, create minimal MCP server in `mcp/server.py` exposing:

```text
search_eval_cases(query)
generate_eval_case(run_id, failure_step, failure_type)
```

Do not spend more than half a day on MCP.

## One-Week Build Plan

### Stage 1

- Create repo
- Add schemas
- Add small MolmoWeb-HumanSkills sample fixtures
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
- Add AGENTS.md and SKILL.md
- Optional minimal MCP server

### Stage 7

- Polish demo
- Add screenshots / GIF
- Run tests
- Produce `evaluation_report.md`
- Prepare README and resume bullets

## README Requirements

README must include:

- What the project is
- Why it exists
- Architecture diagram
- Demo flow
- Setup
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
- Skill-style workflow packaging
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
