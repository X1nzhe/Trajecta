# Eval Agent

The Eval Agent is the core component of Trajecta.

It is implemented as a LangGraph **tool-calling agent**, not a fixed DAG. The agent autonomously decides which steps in a trajectory to deep-dive, when to retrieve failure memory, when to backtrack, and when it has enough evidence to propose an eval case.

The project description emphasizes the Eval Agent capability. LangGraph, ChromaDB, and the multi-resolution VLM strategy are implementation details.

## Design Rationale

Trajectories vary in length (10–80 steps) and in failure mode. A human eval engineer does not analyze every step at full detail. They:

1. Skim the trajectory.
2. Form a hypothesis about where it likely failed.
3. Zoom in on suspicious steps.
4. Cross-reference similar past failures.
5. When useful, pull a successful run of the same task and compare step-by-step to localize the divergence.
6. Sometimes backtrack to earlier steps to find a root cause.
7. Stop when evidence is sufficient.

This is a task with **dynamic information needs and a non-fixed path**, which is the canonical justification for an agent over a deterministic pipeline.

Two boundaries keep the design honest:

- **Trajectory Preprocessing runs the same work on every step.**
  A `for` loop iterates over every step and runs a low-detail VLM (~85 tokens/image) plus action parsing. The VLM call itself is a model invocation — outputs are not bit-identical across runs — but the *orchestration* is fixed: every step is processed, in order, with the same prompt. The output is a cheap text-only **trajectory digest** that the agent consumes. See [docs/preprocessing.md](preprocessing.md) for the schema and contract.
- **High-detail visual inspection is on demand.** Full-resolution VLM analysis (~1500 tokens/image) is expensive. The agent calls `get_step_detail` only for steps it has reason to inspect. *Which* steps to deep-dive is the agent's decision, in contrast to preprocessing where every step is processed unconditionally. This yields a coarse-to-fine pattern with measurable cost savings.

## Pipeline Overview

```text
┌──────────────────────────────────────────────────────────┐
│ Stage 1: Trajectory Preprocessing  (backend/app/preprocess.py)
│   for each step in run:                  ← fixed control flow
│     - low-detail VLM summary (~85 tokens/image)   ← model call
│     - parse action, validate coordinates           ← deterministic
│   → build trajectory_digest: list[StepDigest]   (text only)
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ Stage 2: ★ Eval Agent  (LangGraph tool-calling loop)
│   Input:  trajectory_digest + user intent (Analyze Trajectory / Step)
│   Tools:  get_trajectory, get_step_detail, find_similar_successful_trajectory,
│           search_failure_memory, search_failure_eval_cases, propose_eval_case
│   Loop:   reason → call tool → observe → reason
│   Stop:   agent calls propose_eval_case (terminal) OR
│           tool-call budget reached
└──────────────────────────────────────────────────────────┘
                          ↓
┌──────────────────────────────────────────────────────────┐
│ Stage 3: Human Validation + Export
│   User confirms or edits failure label, then exports JSON.
└──────────────────────────────────────────────────────────┘
```

## Tools

Implement the typed tools from
[docs/contracts.md](contracts.md#agent-tool-contracts) in `backend/app/tools.py`.

Tool design notes:

- `propose_eval_case` is a **terminal tool**. The agent indicates "I have enough evidence" by calling it. The graph transitions to validation when this tool is invoked.
- `get_step_detail` is the only multimodal tool. The agent is expected to call it sparingly (typically 1–4 times per run) on steps surfaced by the digest.
- `find_similar_successful_trajectory` is the replay-and-diff entry point. It returns successful runs of a similar task, excluding the current run when `exclude_trajectory_id` is supplied; the agent then calls `get_trajectory(other_trajectory_id)` (free; not budgeted) to load the comparison digest and reasons about divergence. Calling `get_step_detail` on a step of the comparison trajectory is allowed and counts against the budget normally.
- `retrieved_context_ids` carries the case IDs returned by prior `search_*` calls, providing a traceable link from agent output back to retrieved evidence. Run IDs from `find_similar_successful_trajectory` are **not** stored here; the comparison is traced through `AgentTrace` events.

## LangGraph State

Create `backend/app/eval_agent_graph.py`.

```python
from typing import TypedDict, Optional, List, Dict, Any
from typing import Literal
from langchain_core.messages import AnyMessage


class EvalState(TypedDict):
    trajectory_id: str
    user_intent: Literal["analyze_trajectory", "analyze_step"]
    selected_step: Optional[int]
    trajectory_digest: List[Dict[str, Any]]
    messages: List[AnyMessage]           # agent reasoning + tool-call history
    tool_call_count: int
    eval_case_draft: Optional[Dict[str, Any]]
    errors: List[str]
```

## LangGraph Nodes

The graph is intentionally small. The agent loop is one node that owns the tool-calling cycle; the rest of the graph is plumbing.

```text
START
  → preprocess        # fixed for-loop; per-step VLM call; build trajectory_digest
  → agent_loop        # LLM with tool calls; loops until terminal tool or budget
  → END
```

The `preprocess` graph node is a thin wrapper around `preprocess.load_or_build_digest` — the same function that backs the standalone Pipeline Stage 1 and the `POST /api/trajectories/{trajectory_id}/preprocess` endpoint. There is one implementation; the node, the endpoint, and the pipeline diagram refer to it.

`agent_loop` is a `tools_condition` style cycle: the model produces a message, if it contains tool calls they execute and feed back into the model, otherwise the loop ends. Termination is triggered by either:

- the model calling `propose_eval_case` (success path), or
- `tool_call_count` exceeding the configured budget (`terminated_by="budget_exceeded"` and `errors` is populated).

`propose_eval_case` is a terminal tool. Its schema is enforced by the tool
signature, so no separate validation node is needed; after the tool returns
successfully, the graph sets `eval_case_draft` and routes directly to END.
The agent must not return to the model for another reasoning turn after a
successful terminal call **within the same turn**. A subsequent `/followup`
invocation is a new turn and is allowed to call `propose_eval_case` again.

For `/followup` invocations the graph starts at `agent_loop` and skips
`preprocess` — the digest is already cached from the initial analyze. The
`messages` list is rehydrated from the persisted trace before the loop resumes.

## Screenshot Detail Policy

Two screenshot detail levels exist, and they have different evidentiary weight:

- **Low-detail** (~85 tokens/image) — from `StepDigest.vlm_low_detail_summary` or from `get_step_detail(..., image_detail="low")`. Allowed for orientation, hypothesis formation, and suspicious-step selection.
- **High-detail** (~1500 tokens/image, default) — from `get_step_detail(..., image_detail="high")`. Required for any claim about visual text, button labels, target identity, selected-result constraint satisfaction, or coordinate correctness. The high-detail VLM prompt is task-aware and returns structured fields such as `constraint_evidence`, `selected_candidate`, `success_signals`, and `failure_signals`.

Hard rule: **any field in the final `EvalCase` that depends on visual text, target identity, or coordinate correctness must trace to a high-detail observation** (high-detail `get_step_detail`, OCR, or structured trajectory text such as `StepObservation.visible_text` / `action_target`). Low-detail output may appear in the agent's reasoning, but `EvidenceItem.source="step_detail_low"` or `"trajectory_digest"` must not be the sole support for those final claims.

## Agent Behavior

The system prompt instructs the agent to:

1. Call `get_trajectory(trajectory_id)` once at the start to load trajectory metadata and the digest.
2. Read the `trajectory_digest`, `user_intent`, and optional `selected_step`.
3. Form an initial hypothesis about where the run likely failed.
4. For `analyze_trajectory`, call `get_step_detail` on the most suspicious steps (typically 1–4). Backtrack to earlier steps if the root cause appears upstream.
5. For `analyze_step`, call `get_step_detail(trajectory_id, selected_step)` first, inspect adjacent steps if needed, and still allow backtracking when evidence indicates the root cause is upstream.
6. Call `find_similar_successful_trajectory(task, exclude_trajectory_id=current_trajectory_id)` once a likely failure region is identified. If a comparable success run exists, call `get_trajectory(other_trajectory_id)` and diff the digests step-by-step; use `get_step_detail` on the comparison trajectory only when the digest-level diff is ambiguous.
7. Call `search_failure_memory` and/or `search_failure_eval_cases` with queries grounded in observed evidence — including divergence patterns surfaced by replay-and-diff.
8. When evidence is sufficient, call `propose_eval_case`. Two valid call shapes (the EvalCase schema enforces XOR — half-populated drafts raise):
   - **Failure verdict**: pass all five failure fields (`failure_step`, `failure_type`, `expected_behavior`, `actual_behavior`, `regression_rule`) plus `evidence` and `retrieved_context_ids`.
   - **Success verdict** ("no failure found"): omit all five failure fields; pass only `evidence` and `retrieved_context_ids`. The case_id is generated in the `ec_{trajectory_id}_success` namespace and a second success case for the same run returns 409 on validation.
9. Never invent evidence. If a screenshot, coordinate, or successful comparison trajectory is missing, include an `EvidenceItem` with `source="unavailable"` and a claim that states what was unavailable.

The agent is constrained by a **per-turn** tool-call budget to bound cost and latency:

- **Initial analyze** (`/analyze` or `/steps/{i}/analyze`): default `8`.
- **Follow-up turn** (`/followup`): default `8`. Same as initial — a single follow-up may include a full re-analysis (e.g. user asks the agent to reconsider with new information; agent re-inspects N steps and revises the draft). Per-turn isolation still applies; cost is bounded per turn, not across the whole conversation.

Budget accounting:

- Counts: `get_step_detail`, `search_failure_memory`, `search_failure_eval_cases`, `find_similar_successful_trajectory`.
- Does not count: `get_trajectory`, `propose_eval_case`.
- `get_trajectory` is free even when called on a comparison trajectory returned by `find_similar_successful_trajectory`, but any `get_step_detail` call against that comparison trajectory counts normally.
- The budget resets at the start of each turn. Exceeding the per-turn budget terminates **only that turn** with `terminated_by="budget_exceeded"`; the user may still send another follow-up. `AgentTrace.tool_call_count` keeps incrementing across turns so the total cost remains visible.
- The comparator semantic is `tool_call_count_for_this_turn < budget`: an in-flight tool call is allowed when the pre-call count is strictly less than the budget. The Nth budgeted call is permitted; the (N+1)th is rejected.

## Follow-up Mode

After the initial analyze has produced a trace, the user may ask follow-up questions via `POST /api/trajectories/{trajectory_id}/followup`. This is a second-and-onward turn over the same trace; it is **not** a fresh agent run.

### Lifecycle

1. The handler loads the persisted trace via `storage.load_trace(trajectory_id)`. If absent (no `traces` row for this `trajectory_id`), returns `409` — follow-up has no meaning before an analyze.
2. The handler appends one `AgentTraceEvent(type="user_message", message=..., turn=prior_turn_count)` with the next `seq`.
3. The handler resumes the `agent_loop` node with the existing `messages` (LangGraph state-continuation), reset per-turn budget counter, and the new user message attached as the latest entry.
4. The loop runs the standard `reason → call tool → observe → reason` cycle, appending new events with the same `turn` value.
5. The turn ends by the standard termination conditions:
   - Agent calls `propose_eval_case` → the new draft **replaces** the previous draft in the response. `terminated_by="propose_eval_case"`. `AgentTrace.turn_count` increments.
   - Per-turn budget exhausted → `terminated_by="budget_exceeded"`. The user may follow up again.
   - Terminal tool validation error → `terminated_by="error"`. The user may follow up again to correct.
6. The updated trace is written back via `storage.save_trace(trajectory_id, trace)`, replacing the previous `traces` row inside one transaction.

### Prompt context

The agent's system prompt for follow-up turns must explicitly state:

- The user is asking a follow-up about the previous analysis. The earlier `messages` (including the prior `propose_eval_case` call) are visible in context.
- A follow-up is allowed to revise the earlier eval case by calling `propose_eval_case` again. The new draft fully replaces the old one — there is no merge.
- Targeted tool use is preferred over fresh broad exploration when the user's question is narrow — the per-turn budget bounds cost but does not forbid a re-analysis when the user explicitly asks for one.
- If the user's follow-up is a clarification question (no new evidence needed), the agent should answer in a single `agent_message` and not call `propose_eval_case`.

### Invariants

- A trace may contain multiple `propose_eval_case` tool calls (one per turn that terminates that way). The **latest** call's args define the current draft. RAGAS and the frontend both read the latest call.
- `EvalCase.retrieved_context_ids` returned by a follow-up `propose_eval_case` may reference search results from **any earlier turn** — the whole trace is the evidence pool. The existing invariant ("every retrieved_context_id appears in some search\_\* tool\_result of the same trace") naturally extends.
- `user_intent` and `selected_step` are set by the initial analyze and never modified by follow-up. The framing of the original invocation is preserved for observability and RAGAS sampling.

## Failure Handling

- If `propose_eval_case` raises a Pydantic `ValidationError` or contract error, record an `AgentTraceEvent(type="tool_error")`, append the error text to `EvalState.errors`, set `terminated_by="error"`, and end the **current turn**. v1 does not retry within a turn — the user re-triggers analyze, or sends a follow-up message to correct.
- If the per-turn budget is exceeded, set `terminated_by="budget_exceeded"`, append a budget error to `EvalState.errors`, and end the current turn without modifying the eval case draft.

Errors are populated for budget exhaustion and terminal-tool errors. "Ending the turn" means the graph returns to the API handler; it does not invalidate the trace or block follow-up.

## Offline Agent Mock

Tests must not depend on a live LLM. When no usable LLM credentials are
configured, `eval_agent_graph.py` should use a deterministic mock agent:

1. Call `get_trajectory(trajectory_id)`.
2. For `analyze_step`, call `get_step_detail(trajectory_id, selected_step)`.
3. For `analyze_trajectory`, call `get_step_detail` on the first failed step in the digest, or step 0 if no failed step is present.
4. Call `find_similar_successful_trajectory(task, top_k=1, exclude_trajectory_id=current_trajectory_id)`. If the result is non-empty, call `get_trajectory(result[0]["trajectory_id"])` to exercise the comparison path. If empty, skip silently.
5. Call `search_failure_memory("missed_constraint", top_k=1)`.
6. Call `propose_eval_case(...)` using the returned first case ID as `retrieved_context_ids[0]`.

This mock exists only to stabilize pytest coverage for graph control flow,
retrieval traceability, budget handling, and schema validation. It is not used
for demo-quality analysis.

## Agent Output Schema

The output is the `EvalCase` Pydantic model from
[docs/contracts.md](contracts.md#schema-contracts), populated by the
`propose_eval_case` terminal tool. The agent does **not** emit free-form JSON;
the schema is enforced by the tool signature. `EvalCase.evidence` entries are
structured `EvidenceItem` objects so the UI and tests can trace each claim back
to a run step, tool event, retrieved context, or explicit unavailable evidence.

## Observability

Every agent run produces a structured `AgentTrace` (schema in
[docs/contracts.md](contracts.md#schema-contracts)) covering every tool call,
tool result, and the termination reason. The trace is built directly from
LangGraph's `messages` state at the end of `agent_loop`; there is no separate
observability layer.

Persistence and consumers:

- Persisted as the `traces` row keyed by `trajectory_id`, written by `storage.save_trace`. Overwritten on each `/analyze` (fresh trace) and on each `/followup` (in-place update with appended events). Older traces are not retained in v1.
- Returned in full on `POST /api/trajectories/{trajectory_id}/analyze` and `POST /api/trajectories/{trajectory_id}/followup`.
- Rendered by the frontend `EvalAgentPanel` as a chat-style timeline (`user_message`, `agent_message`, `tool_call` / `tool_result` summaries), grouped by `turn`. See [docs/frontend.md](frontend.md).
- Read by `ragas_eval.py`. The latest `propose_eval_case` tool-call args provide the RAGAS `answer` ([docs/testing.md](testing.md)). All `tool_result` events whose `name` is `search_failure_memory` or `search_failure_eval_cases` — **across all turns of the trace** — provide the retrieved contexts. RAGAS must not re-run retrieval.
- New traces stamp `prompt_version` and `prompt_sha256` from the active prompt bundle under `prompts/eval_agent/`. Follow-ups reuse the trace's prompt version so a resumed analysis stays reproducible.
- High-detail `get_step_detail` tool results stamp `vlm_prompt_version` and `vlm_prompt_sha256` from `prompts/vlm_high_detail/`, so the generated visual evidence is reproducible too.

Invariants enforced in tests:

- Every `case_id` in the proposed `EvalCase.retrieved_context_ids` must appear in some `tool_result` event of the same trace, regardless of which turn produced it.
- Every `EvidenceItem` with `source="failure_memory"` or `"eval_case"` must carry a `context_id` that appears in a prior retrieval tool result.
- Every `EvidenceItem` with `source="step_detail_high"` or `"step_detail_low"` should carry the `trace_event_seq` for the matching `get_step_detail` tool result.
- `AgentTraceEvent.seq` is strictly monotonic across the whole trace.
- `AgentTraceEvent.turn` is non-decreasing across the event list.

Screenshot bytes are never written to the trace. `get_step_detail` results carry a URL plus text fields only; high-detail results include `task_context`, `vlm_prompt_version`, and `vlm_prompt_sha256` so the trace records which task/action/url/title context and prompt produced the VLM summary.

## Cost Strategy (Coarse-to-Fine VLM)

| Stage | VLM detail | Tokens per image | Typical calls per run |
| --- | --- | --- | --- |
| `preprocess` | low | ~85 | at most one per step (skipped when `visible_text` is present) |
| `get_step_detail(image_detail="low")` | low | ~85 | 0–N, agent-decided, for re-orientation on suspicious steps |
| `get_step_detail(image_detail="high")` | high | ~1500 | 1–4 per run |

For a 30-step run where the dataset provides no `visible_text`, preprocessing costs `30 * 85 = 2550` visual tokens and a typical analyze adds `3 * 1500 = 4500`, for a total of `7050` — versus `30 * 1500 = 45000` for a naive full-detail pass. If the dataset already provides DOM text for every step, preprocessing's VLM cost drops to zero and analyze cost stays roughly the same. The cost ablation is part of the README demo.

The agent prompt is laid out with the stable prefix first — system prompt, then the trajectory digest — followed by the dynamic tool-call turns. v1 does not wire provider-specific cache controls; this layout exists so that a caching-capable provider benefits transparently if used, but the cost story does not depend on it.

## MCP Exposure

MCP exposure shipped in Phase 8 B1, after the Gemini judge agreement path. The
entire `agent_loop` described above should be reachable via the `analyze_trajectory`
tool in `trajecta_mcp/server.py` (shipped in Phase 8 B1). External coding agents (Claude
Code, Cursor) would invoke the full LangGraph cycle as a single MCP call rather
than orchestrating individual tools across the MCP boundary. Per-turn budget,
trace integrity, prompt-version stamping, and the HITL gate should all apply
unchanged across MCP invocations; the only observable difference is
`AgentTrace.source == "mcp"`.

`trajecta_mcp/server.py` is a thin transport adapter built on the standalone
`fastmcp` package — it does not duplicate any logic in this file. Tools
are registered via `@mcp.tool()` decorators; the `analyze_trajectory` tool
delegates directly to `eval_agent_graph.analyze_trajectory(..., source="mcp")`.
See [docs/mcp.md](mcp.md) for the tool surface, the include/exclude
rationale, and the rationale for exposing the loop as a composite rather
than as raw tools.

## Skill

The Skill wrapper is optional packaging around the Eval Agent. It is not part
of the V1 closeout.

If future work reopens this track, create one skill file:

`skills/create-eval-case/SKILL.md`

```md
---
name: create-eval-case
description: Use when a browser-agent trajectory has a suspected or labeled failure and should be converted into a reusable regression eval case.
---

# Create Eval Case

## Inputs
- trajectory_id
- optional failure_step
- optional human_note

## Procedure
1. Invoke the Eval Agent on the run.
2. Let the agent inspect suspicious steps, retrieve similar failure memories, and propose an eval case draft.
3. Require human validation before marking the case as final.

## Output
A validated `EvalCase` JSON matching the schema in `docs/contracts.md`.
```
