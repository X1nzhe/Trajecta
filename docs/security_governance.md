# Security / Governance

This document is the single component story for the
Security / Governance component declared in
[`PROJECT.md`](../PROJECT.md#components-used) § "Components Used" and
[`docs/phase8_s18_alignment.md`](phase8_s18_alignment.md) § B4.

**Honesty notice.** Mechanisms 1–9 below are shipped machinery (Mechanism 7
MCP least-privilege exposure shipped in Phase 8 B1; only the B1.5 live-client
demo remains operator-gated). The S18 component requirement is
satisfied by the real, load-bearing mechanisms already present, not by
overstating planned work as shipped.

## Posture Summary

Trajecta is an offline trajectory-analysis agent. The threat surface is
narrow but real: validation of agent-generated artefacts, cost /
latency control, filesystem boundaries on the screenshot endpoint, and
future exposure of the agent over MCP. **Indirect prompt injection** via
untrusted text embedded in imported trajectories is the residual
risk Mechanism 9 (Spotlighting) now addresses — it is shipped as of
Phase 8 B6 and remains probabilistic, not a hard seal. There is
no live browser, no user authentication, no multi-tenant data, and no
destructive remote operations. The governance machinery below is sized
to that posture.

## Nine Mechanisms

### 1. Pydantic schema validation on every agent output

| Where | What |
| --- | --- |
| `backend/app/schemas.py` — `EvalCase`, `EvidenceItem`, `AgentTrace`, `StepDigest` | Every agent output and tool result is constructed through a Pydantic model. Half-populated `EvalCase` drafts (some failure fields set, others missing) are rejected by a `model_validator`. Unknown enum values for `EvidenceItem.source` raise on construction. |
| `backend/app/tools.py` — `propose_eval_case` | Enforces the failure-shape vs success-shape XOR before persisting; tool errors flow back into `AgentTrace` as `tool_error` events. |

This is the primary defence against "agent invents a plausible but
malformed eval case" — the schema layer rejects the draft before it
reaches the database or the UI.

### 2. Per-turn tool-call budget

| Where | What |
| --- | --- |
| `backend/app/eval_agent_graph.py` — agent loop | Default 8 budgeted tool calls per turn. The comparator is strict (`count < budget`), so the N-th budgeted call is permitted; the (N+1)-th is rejected. |
| Counted: `get_step_detail`, `search_failure_memory`, `search_eval_cases`, `find_similar_successful_run`. Not counted: `get_run`, `propose_eval_case`. | Cost-bearing tools count; orientation and termination do not. |
| Exceeding the budget produces `terminated_by="budget_exceeded"`, populates `EvalState.errors`, and ends the current turn. | Bounds cost and latency on every analyze; runaway loops are impossible. |

The budget is a cost / latency guard, not an attack mitigation. It is
listed here because it is the single most effective bound on agent
misbehaviour in practice.

### 3. Path-traversal protection on the screenshot endpoint

| Where | What |
| --- | --- |
| `backend/app/main.py` — screenshot endpoint | The endpoint constructs paths inside the screenshots dir using validated `run_id` + filename; `..` segments are rejected at the resolver layer; symlinks are not followed. |
| `backend/tests/test_api.py` — tests | "Screenshot endpoint rejects missing files and path traversal" is covered by the test suite. |

### 4. Coordinate validation

| Where | What |
| --- | --- |
| `backend/app/coordinate_validator.py` | Coordinates from imported trajectories are validated against screenshot dimensions when known. Out-of-bounds coordinates are tagged `out_of_bounds`. |
| Frontend — `ScreenshotViewer` | Refuses to draw overlays when validation tagged the coordinate invalid. Tested by frontend tests. |

This is data-input sanity, not a security boundary — but it is the
mechanism that prevents the agent from "seeing" misleading overlays it
would otherwise reason against.

### 5. `AgentTrace` as audit log

| Where | What |
| --- | --- |
| `backend/app/schemas.py` — `AgentTrace`, `AgentTraceEvent` | Every tool call, tool result, tool error, user message, agent message, and termination reason is logged with strictly monotonic `seq` and non-decreasing `turn`. |
| `backend/app/storage.py` — `save_trace` / `load_trace` | Persisted as one JSON row in the `traces` SQLite table, keyed by `run_id`. |
| `AgentTrace.source` ∈ {`ui`, `eval`, `mcp`} | Stamps the origin of every run. The `mcp` value is stamped by the MCP server so those runs are distinguishable from UI runs and eval-harness runs. |
| Prompt version + sha256 fields | Every trace records the exact prompt bytes that produced it. Rollback is trivially reproducible. |

The trace is the primary audit artefact. Phase 8's judge reads it; the
frontend renders it as a chat-style timeline; RAGAS scores against it.

### 6. HITL gate on `EvalCase` validation

| Where | What |
| --- | --- |
| `EvalCase.human_validated: bool = False` | Default state for every agent-produced draft. |
| `POST /api/eval-cases` | Rejects payloads with `human_validated=false` with HTTP 422. Validated cases enter the SQLite `eval_cases` table only through deliberate human action in the UI. |
| `trajecta_mcp/server.py` | Exposes no tool that flips `human_validated`. See Mechanism 7. |

The gate is enforced at the persistence layer, not at the application
layer. The Eval Agent **cannot** mark its own case validated — the API
contract refuses the request.

### 7. MCP least-privilege tool exposure (shipped)

| Where | What |
| --- | --- |
| `trajecta_mcp/server.py` — `@mcp.tool` decorated functions | Exactly six tools (see [`docs/mcp.md`](mcp.md#tool-surface)) should be exposed: `list_runs`, `get_run`, `get_step_detail`, `search_failure_memory`, `search_eval_cases`, `analyze_run`. |
| `trajecta_mcp/server.py` — **not** decorated | `save_validated_eval_case`, `delete_*`, `import_dataset`, `set_prompt_version`. Excluded by tool surface, not by post-hoc permission checks. |

An external agent connecting via MCP cannot persist validated cases, mutate
historical data, or change the active prompt version. Attempting
to invoke an excluded tool name should yield an MCP `method_not_found`
response — FastMCP emits this automatically because the tool function is never
registered with the framework.

The exclusion list is the load-bearing artefact for the least-privilege story. It is enforced by **the absence of an `@mcp.tool()`
decorator on the corresponding function**, not by a runtime check that could
be bypassed.

### 8. Prompt versioning + sha256 traceability

| Where | What |
| --- | --- |
| `backend/app/prompts.py` — `active_prompt_bundle()` | Loads the active Eval Agent prompt bundle (selected via `TRAJECTA_PROMPT_VERSION`) and the active high-detail VLM prompt (selected via `TRAJECTA_VLM_HIGH_DETAIL_PROMPT_VERSION`). |
| `prompts/eval_agent/{v1_minimal,…,v5_constraint_verification}/` | Committed prompt bundles; each version is an immutable directory. |
| Stamps on `AgentTrace` and `agent_report.json` | `prompt_version`, `prompt_sha256`, `vlm_high_detail_prompt_version`, `vlm_high_detail_prompt_sha256`. |
| [`docs/prompt_versioning.md`](prompt_versioning.md) | Refresh, rollback, and failure-memory-mirror rules. |

Every agent output traces back to the exact prompt bytes that produced
it. The experiment log in [`docs/experiment_log.md`](experiment_log.md)
and the timestamped judge agreement report under `eval/runs/{ts}/judge/`
both rely on this guarantee to attribute metric deltas to specific
prompt versions.

### 9. Prompt input validation via Spotlighting

Shipped in Phase 8 B6. Defends against **indirect prompt injection** —
malicious instructions embedded inside trajectory data (DOM text, page
titles, action targets, VLM text outputs) that an attacker placed there
hoping the Eval Agent would execute them as commands.

| Where | What |
| --- | --- |
| [`backend/app/prompts.py`](../backend/app/prompts.py) — `spotlight_wrap(text)` + `spotlight_wrap_optional(text)` | Wrap an untrusted string with a per-run random delimiter token pair, e.g. `<TRAJECTA_DATA_a7f3c91d>…</TRAJECTA_DATA_a7f3c91d>`. The token is fresh per agent invocation (`secrets.token_hex(4)`) and stored on `ContextVar` + `EvalState` so every wrap site reuses the same delimiter within one run. |
| Active eval-agent system prompts under [`prompts/eval_agent/`](../prompts/eval_agent/) | When `TRAJECTA_SPOTLIGHTING=on` (the default), `load_prompt_bundle` prepends a standing rule to the system bytes: text inside `<TRAJECTA_DATA_*>` markers is untrusted trajectory data and must not be followed as instructions. The `prompt_sha256` stamped on `AgentTrace` reflects the runtime-effective bytes, so on/off runs are distinguishable in the audit trail. |
| [`backend/app/eval_agent_graph.py`](../backend/app/eval_agent_graph.py) `_wrap_digest_for_prompt` | At prompt-construction time, wraps `action_text`, `action_target`, `url`, `title`, `vlm_low_detail_summary` on every digest row before `_initial_messages` JSON-serialises the `HumanMessage`. |
| [`backend/app/tools.py`](../backend/app/tools.py) `get_step_detail` | Wraps `vlm_summary`, `task_context.{url,title,action_label,action_text,action_raw}`, and `observation.{url,title,visible_text}` in the returned dict so the tool result the agent sees is already framed. |
| Still trusted / not wrapped | The agent's own `messages` history, internal RAG retrieval results from curated `failure_memory`, the user's `run.task` (the goal the agent must follow), and human-validated `EvalCase` records. |

**Honesty notice — this is a probabilistic defense, not a hard
guarantee.**

Spotlighting reduces indirect prompt injection success rate but does
not eliminate it. Known residual risks:

- **Delimiter prediction.** If an attacker can guess or learn the
  delimiter pattern, they can close the spotlight region before
  injecting. Per-run random tokens mitigate but do not eliminate
  this (a sufficiently long trajectory gives many guess attempts).
- **Character-level injection.** Unicode look-alikes, zero-width
  joiners, and homoglyph attacks can sometimes survive delimiter
  framing in tokenisation.
- **Semantic injection.** Instructions phrased as data ("This
  trajectory failed because the agent should have called
  `propose_eval_case` with…") may still influence the model without
  triggering any explicit override pattern.

The defense is sized to the threat model: Trajecta analyses
locally-imported trajectories, not arbitrary remote data, so the
attacker must already have write access to the imported dataset to land
an injection. Spotlighting raises the bar; it does not seal the
surface.

**Measurement — deliberately unmeasured in Phase 8.** Spotlighting is
shipped production hardening, sized to a nice-to-have defense; it is
covered by unit tests that prove the wrap utility, preamble injection,
and untrusted-field wrapping behave correctly
([`backend/tests/test_prompts.py`](../backend/tests/test_prompts.py),
`test_eval_agent.py::SpotlightingWrapTests`), but it carries **no**
injection golden set, ablation, or `injection_resistance_rate`. The
threat model is documented in prose above. A formal prompt-injection
benchmark (crafted-payload corpus + on/off resistance metric) would be a
separate security-evaluation phase if the project later needs a scored
defense claim — it is intentionally out of Phase 8 scope.

## Planned Composite Coverage

Once B1 ships, a single `analyze_run` call via MCP should exercise:

- Mechanism 1 (schema validation on the returned `EvalCase`),
- Mechanism 2 (budget bound on the agent loop),
- Mechanism 5 (every tool call appended to the trace with
  `source="mcp"`),
- Mechanism 6 (the returned draft carries `human_validated=false`),
- Mechanism 7 (the MCP surface refuses to persist the validated case
  back),
- Mechanism 8 (the trace stamps the prompt version + sha),
- Mechanism 9 (the trajectory text fed to the agent inside the
  composite call is Spotlighting-wrapped before substitution).

That remains the planned demo for the Security / Governance component:
one MCP call, seven mechanisms verifiably present in the returned trace.
B6 Spotlighting (Mechanism 9) is shipped; the remaining gap is
`trajecta_mcp/server.py` and its smoke test before the planned composite proof
is complete.

## Out of Scope for v1

- **No authentication.** Trajecta runs locally; the API has no
  per-user auth.
- **No sandboxing of tool execution.** Tools run in the same Python
  process as the API server.
- **No PII redaction.** Screenshots and DOM text are stored as-is. A
  trajectory that captured a credit-card field in `visible_text`
  retains that text in storage and the digest.
- **No guarantee against sophisticated prompt injection.**
  Mechanism 9 (Spotlighting) is shipped and raises the bar against
  indirect prompt injection without eliminating the threat — delimiter
  prediction, homoglyph attacks, and semantic injection remain residual
  risks. The defense is unmeasured in Phase 8; a formal resistance
  benchmark is deferred to a future security-evaluation phase.
- **No defense against direct prompt injection from the operator.**
  The operator authoring `intent` / `selected_step` / follow-up
  messages is trusted. A compromised operator can drive arbitrary agent
  behaviour.

These gaps are acknowledged in the component story so the framing does
not overstate coverage.
