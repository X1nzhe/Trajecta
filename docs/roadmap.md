# Roadmap

## MCP

MCP remains a planned **Phase 8** item, but it is lower priority than the
LLM-judge agreement path. Do not describe it as shipped until `mcp/server.py`
exists and the live client smoke test passes.

The planned `mcp/server.py` will expose six tools (`list_runs`, `get_run`,
`get_step_detail`, `search_failure_memory`, `search_eval_cases`,
`analyze_run`); persistence and destructive operations are deliberately
excluded. The load-bearing tool is `analyze_run`, a **composite** that wraps the
entire LangGraph Eval Agent loop as a single MCP call — not a transport wrapper
around individual tools.

Full design is in [docs/mcp.md](mcp.md). The exclusion list is also the primary
least-privilege artefact in [docs/security_governance.md](security_governance.md)
§ Mechanism 7.

## One-Week Build Plan

### Phase 1 - Done

- Create repo
- Add schemas
- Add at least 5 small MolmoWeb-HumanSkills sample fixtures
- Commit `data/raw/molmoweb_humanskills_sample/run_status_overlay.json` covering those fixtures (≥1 `success` entry per task category — the overlay file is retained as a historical artifact; v1 cold-start ignores it, see [docs/dataset_import.md](dataset_import.md) "Cold-Start Behavior")
- Seed one example validated `EvalCase` so `GET /api/eval-cases` is non-empty on a fresh clone (originally committed as `data/eval_cases/validated/{case_id}.json`; now seeded into the SQLite `eval_cases` table via `storage.save_eval_case`)
- Seed one example `AgentTrace` (originally a `last_trace.json` fixture; superseded by the SQLite `traces` table after the storage refactor — frontend dev now seeds via `storage.save_trace` against a temp DB)
- Add failure memory cases
- Add basic tests

### Phase 2 - Done

- Implement dataset importer
- Implement coordinate validator
- Implement backend storage and tools

### Phase 3 - Done

- Implement Trajectory Preprocessing (`preprocess.py`) per [docs/preprocessing.md](preprocessing.md): build the `trajectory_digest` with low-detail VLM hints, parsed actions, and coordinate validation
- Implement ChromaDB RAG (`failure_memory` + `eval_cases` + `successful_runs` collections)
- Implement the LangGraph tool-calling Eval Agent with `get_run`, `get_step_detail`, `find_similar_successful_run`, `search_failure_memory`, `search_eval_cases`, and the terminal `propose_eval_case` tool
- Convert the agent loop's final `messages` into an `AgentTrace` and persist via `storage.save_trace(run_id, trace)` (writes the `traces` SQLite row, overwritten each analyze; originally written as `data/runs/{run_id}/last_trace.json` pre-SQLite refactor)

### Phase 4 - Done

- Add RAGAS eval script
- Finish pytest coverage

### Phase 5 - Done

- Build React UI
- Run list
- Step timeline
- Screenshot viewer
- Step details (Action / Observation / Coordinate Validation / Metadata tabs)

### Phase 6 - Done except SKILL.md and MCP server

- Add Eval Agent panel as a chat-style timeline (renders trace events grouped by turn)
- Wire Analyze Run / Analyze Step as the only fresh-trace entrypoints
- Implement `POST /api/runs/{run_id}/followup` endpoint and the chat input + prompt chips that drive it (~+1-1.5 days vs. button-only design)
- Termination badge, View Draft / Mark validated / Export eval case flow
- Visual-only thumbs feedback (not wired)
- Footer dataset/run summary
- Add SKILL.md if time permits
- Optional minimal MCP server

### Phase 7 - Done

- Polish demo
- Add screenshots / GIF
- Run tests
- Produce `ragas_report.md` (stub fallback; Phase 8 A6 makes it real)
- Production agent-quality eval harness (`backend/app/agent_eval.py`) with
  binary verdict accuracy + per-category breakdown + cost ablation
- Versioned prompt registry (`prompts/eval_agent/v1_minimal` →
  `v5_constraint_verification`, `prompts/vlm_high_detail/v1_task_context`) with
  sha256 stamping on every trace and report
- Prepare README and resume bullets

### Phase 8 — S18 Capstone Alignment

Operating spec: [docs/phase8_s18_alignment.md](phase8_s18_alignment.md).

**8.A — Eval Deliverables**
- `eval/golden.jsonl` (35 cases, S18-mandated schema, built from `data/triage_notes.csv`)
- `agent_eval.py --trace-dir` flag for per-sample trace persistence (`eval/runs/{ts}/traces/`)
- `agent_eval.py --judge` post-step that runs `eval/judge.py` over the generated `eval_case_draft`
- `eval/judge.py` Gemini-compatible and OpenAI-compatible LLM judges, with concrete models supplied by `TRAJECTA_JUDGE_A_MODEL` and `TRAJECTA_JUDGE_B_MODEL`, binary `acceptable_eval_case` verdicts, and acceptability assertions
- A4.2 provider-specific prompt-bundle todo: create bundles such as `prompts/judge/v1_acceptability_gemini/` and `prompts/judge/v1_acceptability_openai/`, or document reuse of existing bundles if implementation chooses shared prompt + provider adapters
- κ_LLM,LLM table in `eval/judge_report.md`, computed between Gemini and OpenAI verdicts; preferred N=31 gradeable cases, with deterministic pre-registered stratified subsets allowed for cost-constrained judge runs when `sample_size` and `selection_policy` are reported; disagreement analysis when κ < 0.6
- A human second judge is deferred because reviewer workflow, UI, and label-management design would add implementation scope beyond Phase 8; no frontend/API judge-review mode is required
- Real RAGAS (path bug fix + run against persisted traces, `mode == "real"`, `n ≥ 10`)
- `docs/experiment_log.md` + README table — v1→v5 metric deltas
- `docs/failure_analysis.md` — 2–3 cases + one-line trade-off

**8.B — Planned MCP + Component Story** (lower priority than 8.A judge work)
- `mcp/server.py` — planned six-tool server, `analyze_run` composite, deliberate exclusions
- `docs/mcp.md` — design source of truth
- `docs/security_governance.md` — nine-mechanism framing (eight existing + Spotlighting B6)
- B6 — Spotlighting Delimiting defense against indirect prompt injection: `spotlight_wrap()` utility, anti-injection preamble in system prompt, `eval/injection_golden.jsonl` (≥ 8 crafted cases), baseline-vs-on ablation in `docs/experiment_log.md`

**8.C — Tactical Cleanup**
- Frontend TypeScript build fix (`cd frontend && npm run build` exits 0)
- Working-tree-clean pass before the 48-hour push
- (RAGAS path fix absorbed into 8.A.6)

**Not in Phase 8** (rationales in [PROJECT.md](../PROJECT.md#design-decisions) Decisions 7–10):
- No Reviewer Agent / supervisor architecture
- No Mem0 / Letta / Graphiti memory framework
- No Langfuse / Inspect AI tracing
- No CI threshold gate (deferred)

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

Draft after completion; do not use a bullet until the matching tracker slices
are actually done:

- Built **Trajecta**, an AI-native Eval Agent for browser-agent trajectory evaluation that converts raw trajectories into human-reviewable regression eval cases. Filled the missing layer between browser-control MCP servers (browser-use, Browserbase) and trajectory datasets (MolmoWeb-HumanSkills, WebArena) — a remote callable agent that diagnoses failures with retrieval-grounded evidence.
- Designed and implemented a **LangGraph tool-calling agent** with multi-turn follow-up: the agent autonomously decides which trajectory steps to deep-dive, when to retrieve failure memory, and when to terminate via a typed `propose_eval_case` tool — users can ask follow-up questions that resume the same trace under a smaller per-turn budget, with the agent free to revise the draft.
- Designed a lower-priority **MCP composite** plan to expose the entire LangGraph Eval Agent as a single `analyze_run` tool once implemented. The design keeps external coding agents on one remote call that internally orchestrates RAG retrieval, coarse-to-fine VLM inspection, and structured `EvalCase` proposal — while persistence and validation remain HITL-gated outside the MCP boundary.
- Reduced visual-token cost ~80 % via a **coarse-to-fine VLM strategy**: Trajectory Preprocessing calls a low-detail VLM (~85 tokens/image) on every step to build a digest, while high-detail VLM is invoked on demand by the agent only for steps it flags as suspicious. Measured the savings end-to-end against the naive all-steps-high-detail baseline.
- Built **ChromaDB-backed RAG** over failure memory and prior eval cases, with agent-authored queries and traceable `retrieved_context_ids` linking each generated case back to its supporting evidence.
- Designed and implemented an **LLM-judge eval harness** wired as an `agent_eval` post-step, scoring generated eval case drafts as `acceptable_eval_case` over a 35-case golden set with env-configured Gemini-compatible and OpenAI-compatible judges; reported Cohen's κ between the two LLM judges, with disagreement analysis when κ < 0.6 rather than relaxing the judge contract.
- Shipped a **Spotlighting prompt input validation** defense (Hines et al. MSR 2024) against indirect prompt injection in browser-trajectory text — per-run random delimiter tokens, anti-injection preamble in the system prompt, and a small `injection_golden.jsonl` eval reporting `injection_resistance_rate` against a baseline ablation. Documented as a probabilistic defense, not a hard guarantee.
- Ran a **prompt-iteration experiment** (v1 → v5) on a real-LLM 31-sample evaluation, logging metric deltas per round and surfacing negative-result rounds; cost-tracked to $0.032/run with mean wall-clock 27.92 s.
- Evaluated retrieval-grounded analysis with **RAGAS faithfulness and context-precision** metrics over persisted agent traces.
- Built a **React replay UI** for screenshot-based trajectory inspection, coordinate validation, agent reasoning visualisation, and eval case export.
- Added pytest coverage for schemas, MolmoWeb import, coordinate validation, preprocessing, tools, ChromaDB retrieval, the agent loop, the golden set builder, and the LLM judge.
