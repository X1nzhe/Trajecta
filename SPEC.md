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

## Components Used (S18 § 2.1)

Trajecta uses four of the six S18-listed agent components. Three components used
well — with a clear story for why they are wired together — is the bar; four is
what falls out naturally from this project's shape.

| Component | How Trajecta uses it | Anchor doc |
| --- | --- | --- |
| **RAG** | ChromaDB over three collections (`failure_memory`, `eval_cases`, `successful_runs`). Agent-authored queries; every retrieved case ID surfaces in `retrieved_context_ids` so claims trace back to evidence. | [docs/rag.md](docs/rag.md) |
| **Tools** | LangGraph tool-calling agent with six typed tools (`get_run`, `get_step_detail`, `find_similar_successful_run`, `search_failure_memory`, `search_eval_cases`, `propose_eval_case`). Per-turn budget bounds cost; terminal tool enforces schema. | [docs/eval_agent.md](docs/eval_agent.md) |
| **Security / Governance** | Nine-mechanism stack: schema validation, tool-call budget, path-traversal protection, coordinate validation, `AgentTrace` audit log, HITL persistence gate, MCP least-privilege tool surface, prompt-version + sha256 traceability, and **Spotlighting prompt input validation** against indirect prompt injection in trajectory text. | [docs/security_governance.md](docs/security_governance.md) |
| **MCP** | `mcp/server.py` exposes the entire Eval Agent loop as a composite `analyze_run` tool, plus five read-only / cost-bounded tools. Persistence and destructive operations are deliberately not exposed; the HITL gate stays on the Trajecta-UI side. | [docs/mcp.md](docs/mcp.md) |

**Not claimed**: Multi-agent (Trajecta is one Eval Agent plus a human validator;
HITL is the load-bearing role split but Trajecta does not run a supervisor +
worker architecture), Memory (validated `EvalCase` records function as
lightweight human-curated case memory but Trajecta does not use Mem0, Letta, or
Graphiti and does not claim Memory as a primary component).

## Market Positioning

The browser-agent ecosystem covers two layers today: browser-control MCP
servers (browser-use, Browserbase, Playwright MCP) drive a browser
end-to-end, and trajectory datasets (MolmoWeb-HumanSkills, WebArena,
Agent-Eval-Refine) publish recorded runs.

What is missing is a remote callable agent that takes a recorded
trajectory, diagnoses its failure mode with retrieval-grounded evidence,
and produces a regression-eval-case draft. Trajecta fills that gap. The
MCP composite tool `analyze_run` (see [docs/mcp.md](docs/mcp.md)) is the
remote interface to this missing layer.

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
7. **The MCP server exposes the agent as a composite, not as raw tools.**
   `mcp/server.py` exposes the entire LangGraph loop as a single `analyze_run`
   tool plus five read-only / cost-bounded tools. Splitting the loop across the
   MCP boundary would break the per-turn budget contract and produce disjoint
   traces that RAGAS and the Phase 8 judge cannot score. See
   [docs/mcp.md](docs/mcp.md) "Why expose the whole agent rather than individual
   tools".
8. **No Reviewer Agent / no supervisor architecture in v1.**
   We considered adding a proposer-critic Reviewer Agent to upgrade the
   multi-agent component. Decision: do not add it. Cost: 4–6 hours of work
   plus an additional LLM call per analyze. Benefit: marginal — we already
   reach four S18 components without it (RAG + Tools + Security + MCP), and a
   real Reviewer Agent is a 1–2 week project not a Phase 8 add. Phase 8 instead
   strengthens the existing single-agent loop via judge-driven prompt iteration.
9. **No Mem0 / Letta / Graphiti memory framework in v1.**
   We considered framing the failure-memory mirror as cross-session memory.
   Decision: do not. `failure_memory/cases.jsonl` is a curated RAG knowledge
   base, and validated `EvalCase` rows function as lightweight human-curated
   case memory retrievable via `search_eval_cases`. Calling this "Memory as a
   component" would overstate what is shipped; honesty matters more than
   component count when we already have four.
10. **No Langfuse / Inspect AI in v1.**
    `AgentTrace` (Mechanism 5 in [docs/security_governance.md](docs/security_governance.md))
    covers the observability surface that a third-party tracing tool would
    provide for this project: per-event audit, prompt version stamps, cost
    accounting, run linkage. Adding Langfuse would not change any number in
    the eval report.

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

## Phase 8 — S18 Capstone Alignment

Phase 8 closes the gap to the S18 capstone deliverable: a defendable
eval harness, an LLM judge with measurable inter-annotator agreement, an
experiment log, a failure-analysis writeup, an MCP server that exposes
the agent as a composite tool, and a single-doc treatment of existing
governance machinery.

Phase 8 ships:

- **8.A — Eval rigor.** `eval/golden.jsonl` (35 cases, S18-mandated
  schema); per-sample trace persistence for the judge to consume;
  `eval/judge.py` scoring `acceptable_eval_case` (binary, six-clause
  rubric); Cohen's κ vs both a second LLM judge and a human-labelled
  subset; a real (non-stub) RAGAS run; `docs/experiment_log.md` with
  v1→v5 prompt metric deltas; `docs/failure_analysis.md` with 2–3 case
  studies and the quality / latency / cost trade-off.
- **8.B — MCP + Component story.** `mcp/server.py` with six tools
  including the `analyze_run` composite; [docs/mcp.md](docs/mcp.md);
  [docs/security_governance.md](docs/security_governance.md) framing the
  eight existing governance mechanisms as one cohesive component, plus a
  ninth **Spotlighting prompt input validation** mechanism shipped in B6
  as defense against indirect prompt injection in trajectory text.
- **8.C — Tactical cleanup.** Frontend TypeScript build fix; RAGAS path
  bug fix; repo-hygiene sweep before the 48-hour push.

The operational spec is
[docs/phase8_s18_alignment.md](docs/phase8_s18_alignment.md). That file is
the single point of truth for what Phase 8 ships and when an item is
considered done.

## Documentation Map

| File | Purpose |
| --- | --- |
| [docs/phase8_s18_alignment.md](docs/phase8_s18_alignment.md) | **Phase 8 operating spec.** S18 requirement → deliverable map; acceptance checklist; presentation outline. |
| [docs/product_scope.md](docs/product_scope.md) | Product positioning, v1 scope, non-goals, and core user flow. |
| [docs/architecture.md](docs/architecture.md) | Recommended stack, repository structure, and system boundaries. |
| [docs/contracts.md](docs/contracts.md) | Single source of truth for schemas, tool contracts, API endpoints, RAG collections, and screenshot access. |
| [docs/data_model.md](docs/data_model.md) | Implementation notes for Pydantic schemas defined in `docs/contracts.md`. |
| [docs/dataset_import.md](docs/dataset_import.md) | MolmoWeb-HumanSkills sample import strategy and coordinate validation risk. |
| [docs/preprocessing.md](docs/preprocessing.md) | Trajectory Preprocessing: digest schema, low-detail VLM contract, caching, fallbacks. |
| [docs/eval_agent.md](docs/eval_agent.md) | LangGraph Eval Agent behavior, loop design, observability, and Skill wrapper. |
| [docs/prompt_versioning.md](docs/prompt_versioning.md) | Prompt version registry, traceability, rollback, and failure-memory refresh rules. |
| [docs/rag.md](docs/rag.md) | ChromaDB RAG retrieval strategy. |
| [docs/mcp.md](docs/mcp.md) | MCP server design: tool surface, `analyze_run` composite semantics, client config, demo script. |
| [docs/security_governance.md](docs/security_governance.md) | Nine-mechanism component story for Security / Governance, including the Phase 8 B6 Spotlighting defense against indirect prompt injection. |
| [docs/api.md](docs/api.md) | FastAPI implementation notes for endpoint contracts. |
| [docs/frontend.md](docs/frontend.md) | React UI layout, components, and product copy. |
| [docs/testing.md](docs/testing.md) | pytest, RAGAS evaluation, golden set + judge protocol, and acceptance criteria. |
| [docs/experiment_log.md](docs/experiment_log.md) | v1→v5 prompt-version experiment log; metric deltas; conclusions. |
| [docs/failure_analysis.md](docs/failure_analysis.md) | 2–3 case studies of failed analyses; trade-off statement. |
| [docs/roadmap.md](docs/roadmap.md) | One-week build plan, Phase 8 entry, README requirements, roadmap, and resume bullets. |

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
