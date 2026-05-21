# Frontend UI

Do not overbuild. Single-page app is enough.

Layout:

```text
Left:   Run list
Center: Screenshot replay + step timeline + step detail tabs
Right:  Eval Agent panel (chat-style) + Eval case draft
Footer: dataset and run count summary
```

## Positioning

The right panel uses a chat-style affordance (history + input + prompt chips) because it is the most natural surface for an agent's tool-call trace, the analysis result, and follow-up questions about the result. This is **not a general chatbot** — the agent is bound to one trajectory at a time, operates only through declared tools, and terminates by `propose_eval_case`. The chat surface is a UI presentation choice; the agentic contract in [docs/eval_agent.md](eval_agent.md) is unchanged.

Product wording:

```text
Raw trajectory -> AI-assisted analysis -> Human validation -> Regression eval case
```

Avoid implying:

```text
browser automation agent
generic chat assistant
generic observability platform
```

## Components

### `RunList.tsx`

- List runs from `GET /api/runs`.
- Show run status badge: `failed`, `success`, `unknown`.
- Show task text, date, step count, comment count.
- Run ID rendered truncated on the card (e.g. first 8 + last 4 characters); the full `run_id` is shown in a tooltip on hover so the user can copy it.
- Click loads the run and resets the center and right panels.
- Search box filters by `task` substring; status filter chips (All / Failed / Success / Review).

### `StepTimeline.tsx`

- Show step numbers, action types, and per-step result status.
- Highlight the currently selected step.
- Highlight steps the agent inspected via `get_step_detail` (read from the latest trace).
- Click a step to select it in `ScreenshotViewer` and `StepDetailPanel`.

### `ScreenshotViewer.tsx`

- Show selected step screenshot via `/api/runs/{run_id}/screenshots/{filename}` or an API-provided URL.
- Draw coordinate marker **only if** coordinate validation is `validated`.
- Draw bbox **only when** bbox exists, the screenshot is available, and bbox bounds are validated against screenshot dimensions.
- Playback controls (prev / play / next) iterate over the trajectory's steps.

### `StepDetailPanel.tsx`

Tabs:

- **Action** — `StepAction` fields: type, label, text, coordinates, bounding box, raw, element selector if available.
- **Observation** — `StepObservation` fields: url, title, visible_text, visual_evidence.
- **Coordinate Validation** — `CoordinateValidation` fields: status, image_width, image_height, reason.
- **Metadata** — timestamp + free-form `metadata` map.

Tabs map directly to [docs/contracts.md](contracts.md) schema fields. Do not add Console / Network tabs — Trajecta is not a browser DevTools clone.

### `EvalAgentPanel.tsx` (chat-style)

Renders the agent's trace as a chat-style timeline plus a follow-up input.

**Header**

- Title: `Eval Agent`.
- Termination badge reflecting `AgentTrace.terminated_by` of the latest turn:
  - `propose_eval_case` → neutral / success color.
  - `budget_exceeded` → grey.
  - `error` → red, with the latest `tool_error` message shown on hover.
- A `New Chat` action that calls `/analyze` again with the same `user_intent` (re-runs from scratch — discards the existing trace after a confirm prompt).

**Primary action buttons** (above the chat history, always visible)

- `Analyze this run` → `POST /api/runs/{run_id}/analyze`.
- `Analyze this step` → `POST /api/runs/{run_id}/steps/{step_index}/analyze`. Disabled until a step is selected.

These are the **only** buttons that can start a fresh trace. All other interactions go through the chat input.

**Chat history**

Rendered as an ordered list of bubbles derived from `AgentTrace.events`, grouped visually by `turn`:

- `user_message` → right-aligned user bubble.
- `agent_message` → left-aligned agent bubble (Markdown rendering allowed).
- `tool_call` + matching `tool_result` → collapsed "tool call card" with the tool name, key args, and a one-line result summary. Clickable to expand the full result. For `get_step_detail` tool calls, clicking the card also jumps `StepTimeline` to that step.
- `tool_error` → red tool card with the error text.
- A terminal `propose_eval_case` tool call renders inline with a "View Draft" affordance that scrolls to / opens `EvalCaseDraft`.

When the agent loop is in flight, show a typing indicator at the bottom of the history.

**Prompt chips** (above the chat input)

Six clickable chips that **prefill** the chat input (user can still edit before sending). They do not bypass the agent — they are templates that produce a natural-language message:

| Chip label | Prefill text |
| --- | --- |
| Suggest failure label | `Suggest the failure label for this run.` |
| Generate eval case | `Generate the eval case draft.` |
| Find similar failures | `Find similar failure cases from memory.` |
| Compare with another run | `Compare this run with a similar successful run.` |
| Inspect this step | `Inspect step {selected_step} in detail.` (disabled when no step selected) |
| Explain your reasoning | `Explain why you flagged the failure step.` |

The chip labels and prefill texts are UI strings, not contract values; the agent treats them as ordinary user messages.

**Chat input**

- Single-line text input with a send button. Enter submits; Shift-Enter adds a newline.
- Disabled until the run has at least one trace turn (i.e., user must click `Analyze this run` or `Analyze this step` first). Show a hint: "Run an analysis first to start a conversation."
- On send, calls `POST /api/runs/{run_id}/followup` with `{message}`. Appends the user message to the chat history optimistically; replaces / extends the history with the returned trace events on response.
- Max 2000 characters (matches the API contract).

**Feedback affordance** (visual only in v1)

A thumbs-up / thumbs-down pair next to the latest agent message. Clicking them sets a local highlight state but is not wired to any backend in v1. Wire-up is a v2 task.

### `EvalCaseDraft.tsx`

- Show complete `EvalCase`-shaped draft from the response's `eval_case_draft` field.
- Render every field from the [EvalCase schema](contracts.md#schema-contracts): `failure_step`, `failure_type`, `expected_behavior`, `actual_behavior`, `evidence`, `regression_rule`, `retrieved_context_ids`.
- Allow the user to edit fields inline before export.
- `Mark validated` checkbox sets `human_validated=true` in the client-side draft.
- `Export Eval Case` button is **disabled** while `human_validated=false`. Clicking calls `POST /api/eval-cases` with the validated body.
- If the agent revises the draft via a follow-up `propose_eval_case`, the panel re-renders with the new draft. If the user had unsaved edits, show a confirm prompt before overwriting them.
- Do not display agent confidence scores. LLM self-reported confidence is uncalibrated and misleading; if a grounding signal is needed, derive it from trace structure (evidence count, high-detail observation count, retrieval grounding) — that derivation is a v2 enhancement.

### Footer

Project-level summary row at the bottom of the page:

- Dataset name (e.g., `MolmoWeb-HumanSkills`).
- Imported run count + breakdown by status (e.g., `25 runs · 8 failed · 12 success · 5 unknown`).
- Latest import timestamp if available.

Do not show fake database / schema version labels — Trajecta's storage is local files and ChromaDB; surfacing a schema version is misleading in v1.

## State Rules

- A run's trace lifecycle: `none` → `fresh` (first analyze) → `extended` (one or more follow-ups). The `EvalAgentPanel` infers state from `AgentTrace.turn_count`.
- Switching the selected run discards in-flight chat input but does not delete `last_trace.json`. Re-opening the run reloads the persisted trace from `GET /api/runs/{run_id}` (or the dedicated trace endpoint, whichever is wired up).
- Concurrent follow-ups are not supported in v1 — disable the send button while a request is in flight.
- Draft state is **client-only** until the user clicks `Export`. Page refresh = lose unsaved draft edits.
