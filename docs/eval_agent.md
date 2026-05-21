# Eval Agent

The Eval Agent is the core component of Trajecta.

It is implemented as a LangGraph **tool-calling agent**, not a fixed DAG. The agent autonomously decides which steps in a trajectory to deep-dive, when to retrieve failure memory, when to backtrack, and when it has enough evidence to propose an eval case.

The project description emphasizes the Eval Agent capability. LangGraph, ChromaDB, and the multi-resolution VLM strategy are implementation details.

## Design Rationale

Trajectories vary in length (10–80 steps) and in failure mode. A human eval engineer does not analyze every step at full detail. They:

1. Skim the run.
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
│   Input:  trajectory_digest + user intent (Analyze Run / Step)
│   Tools:  get_run, get_step_detail, find_similar_successful_run,
│           search_failure_memory, search_eval_cases, propose_eval_case
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
- `find_similar_successful_run` is the replay-and-diff entry point. It returns successful runs of a similar task; the agent then calls `get_run(other_run_id)` (free; not budgeted) to load the comparison digest and reasons about divergence. Calling `get_step_detail` on a step of the comparison run is allowed and counts against the budget normally.
- `retrieved_context_ids` carries the case IDs returned by prior `search_*` calls, providing a traceable link from agent output back to retrieved evidence. Run IDs from `find_similar_successful_run` are **not** stored here; the comparison is traced through `AgentTrace` events.

## LangGraph State

Create `backend/app/eval_agent_graph.py`.

```python
from typing import TypedDict, Optional, List, Dict, Any
from typing import Literal
from langchain_core.messages import AnyMessage


class EvalState(TypedDict):
    run_id: str
    user_intent: Literal["analyze_run", "analyze_step"]
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

The `preprocess` graph node is a thin wrapper around `preprocess.load_or_build_digest` — the same function that backs the standalone Pipeline Stage 1 and the `POST /api/runs/{run_id}/preprocess` endpoint. There is one implementation; the node, the endpoint, and the pipeline diagram refer to it.

`agent_loop` is a `tools_condition` style cycle: the model produces a message, if it contains tool calls they execute and feed back into the model, otherwise the loop ends. Termination is triggered by either:

- the model calling `propose_eval_case` (success path), or
- `tool_call_count` exceeding the configured budget (`terminated_by="budget_exceeded"` and `errors` is populated).

`propose_eval_case` is a terminal tool. Its schema is enforced by the tool
signature, so no separate validation node is needed; after the tool returns
successfully, the graph sets `eval_case_draft` and routes directly to END.
The agent must not return to the model for another reasoning turn after a
successful terminal call.

## Screenshot Detail Policy

Two screenshot detail levels exist, and they have different evidentiary weight:

- **Low-detail** (~85 tokens/image) — from `StepDigest.vlm_low_detail_summary` or from `get_step_detail(..., image_detail="low")`. Allowed for orientation, hypothesis formation, and suspicious-step selection.
- **High-detail** (~1500 tokens/image, default) — from `get_step_detail(..., image_detail="high")`. Required for any claim about visual text, button labels, target identity, or coordinate correctness.

Hard rule: **any field in the final `EvalCase` that depends on visual text, target identity, or coordinate correctness must trace to a high-detail observation** (high-detail `get_step_detail`, OCR, or structured trajectory text such as `StepObservation.visible_text` / `action_target`). Low-detail output may appear in the agent's reasoning but must not be cited as the sole source of evidence in `EvalCase.evidence`.

## Agent Behavior

The system prompt instructs the agent to:

1. Call `get_run(run_id)` once at the start to load run metadata and the digest.
2. Read the `trajectory_digest`, `user_intent`, and optional `selected_step`.
3. Form an initial hypothesis about where the run likely failed.
4. For `analyze_run`, call `get_step_detail` on the most suspicious steps (typically 1–4). Backtrack to earlier steps if the root cause appears upstream.
5. For `analyze_step`, call `get_step_detail(run_id, selected_step)` first, inspect adjacent steps if needed, and still allow backtracking when evidence indicates the root cause is upstream.
6. Call `find_similar_successful_run(task)` once a likely failure region is identified. If a comparable success run exists, call `get_run(other_run_id)` and diff the digests step-by-step; use `get_step_detail` on the comparison run only when the digest-level diff is ambiguous.
7. Call `search_failure_memory` and/or `search_eval_cases` with queries grounded in observed evidence — including divergence patterns surfaced by replay-and-diff.
8. When evidence is sufficient, call `propose_eval_case` with all required fields.
9. Never invent evidence. If a screenshot, coordinate, or successful comparison run is missing, say so explicitly in `evidence`.

The agent is constrained by a tool-call budget (default 8) to bound cost and latency.

Budget accounting:

- Counts: `get_step_detail`, `search_failure_memory`, `search_eval_cases`, `find_similar_successful_run`.
- Does not count: `get_run`, `propose_eval_case`.
- `get_run` is free even when called on a comparison run returned by `find_similar_successful_run`, but any `get_step_detail` call against that comparison run counts normally.

## Failure Handling

- If `propose_eval_case` raises a Pydantic `ValidationError` or contract error, record an `AgentTraceEvent(type="tool_error")`, append the error text to `EvalState.errors`, set `terminated_by="error"`, and end the graph. v1 does not retry — the user re-triggers analyze.
- If the budget is exceeded, set `terminated_by="budget_exceeded"`, append a budget error to `EvalState.errors`, and end the graph without an eval case draft.

Errors are populated for budget exhaustion and terminal-tool errors.

## Offline Agent Mock

Tests must not depend on a live LLM. When no usable LLM credentials are
configured, `eval_agent_graph.py` should use a deterministic mock agent:

1. Call `get_run(run_id)`.
2. For `analyze_step`, call `get_step_detail(run_id, selected_step)`.
3. For `analyze_run`, call `get_step_detail` on the first failed step in the digest, or step 0 if no failed step is present.
4. Call `find_similar_successful_run(task, top_k=1)`. If the result is non-empty, call `get_run(result[0]["run_id"])` to exercise the comparison path. If empty, skip silently.
5. Call `search_failure_memory("missed_constraint", top_k=1)`.
6. Call `propose_eval_case(...)` using the returned first case ID as `retrieved_context_ids[0]`.

This mock exists only to stabilize pytest coverage for graph control flow,
retrieval traceability, budget handling, and schema validation. It is not used
for demo-quality analysis.

## Agent Output Schema

The output is the `EvalCase` Pydantic model from
[docs/contracts.md](contracts.md#schema-contracts), populated by the
`propose_eval_case` terminal tool. The agent does **not** emit free-form JSON;
the schema is enforced by the tool signature.

## Observability

Every agent run produces a structured `AgentTrace` (schema in
[docs/contracts.md](contracts.md#schema-contracts)) covering every tool call,
tool result, and the termination reason. The trace is built directly from
LangGraph's `messages` state at the end of `agent_loop`; there is no separate
observability layer.

Persistence and consumers:

- Written to `data/runs/{run_id}/last_trace.json`, overwritten on each analyze. This is enough for the frontend to re-read after navigation; older traces are not retained in v1.
- Returned in full on `POST /api/runs/{run_id}/analyze`.
- Rendered by the frontend `EvalAgentPanel` as the agent's reasoning steps.
- Read by `ragas_eval.py`; `tool_result` events whose `name` is `search_failure_memory` or `search_eval_cases` provide the retrieved contexts for faithfulness scoring. RAGAS must not re-run retrieval.

Invariant enforced in tests: every `case_id` in the proposed `EvalCase.retrieved_context_ids` must appear in some `tool_result` event of the same trace.

Screenshot bytes are never written to the trace. `get_step_detail` results carry a URL plus text fields only.

## Cost Strategy (Coarse-to-Fine VLM)

| Stage | VLM detail | Tokens per image | Typical calls per run |
| --- | --- | --- | --- |
| `preprocess` | low | ~85 | at most one per step (skipped when `visible_text` is present) |
| `get_step_detail(image_detail="low")` | low | ~85 | 0–N, agent-decided, for re-orientation on suspicious steps |
| `get_step_detail(image_detail="high")` | high | ~1500 | 1–4 per run |

For a 30-step run where the dataset provides no `visible_text`, preprocessing costs `30 * 85 = 2550` visual tokens and a typical analyze adds `3 * 1500 = 4500`, for a total of `7050` — versus `30 * 1500 = 45000` for a naive full-detail pass. If the dataset already provides DOM text for every step, preprocessing's VLM cost drops to zero and analyze cost stays roughly the same. The cost ablation is part of the README demo.

The agent prompt is laid out with the stable prefix first — system prompt, then the trajectory digest — followed by the dynamic tool-call turns. v1 does not wire provider-specific cache controls; this layout exists so that a caching-capable provider benefits transparently if used, but the cost story does not depend on it.

## Skill

The Skill wrapper is optional packaging around the Eval Agent. It is not a v1 blocker.

Create one skill file:

`skills/create-eval-case/SKILL.md`

```md
---
name: create-eval-case
description: Use when a browser-agent trajectory has a suspected or labeled failure and should be converted into a reusable regression eval case.
---

# Create Eval Case

## Inputs
- run_id
- optional failure_step
- optional human_note

## Procedure
1. Invoke the Eval Agent on the run.
2. Let the agent inspect suspicious steps, retrieve similar failure memories, and propose an eval case draft.
3. Require human validation before marking the case as final.

## Output
A validated `EvalCase` JSON matching the schema in `docs/contracts.md`.
```
