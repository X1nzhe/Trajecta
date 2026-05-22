# Frontend UI

Do not overbuild. Single-page app is enough.

Layout:

```text
Left:   Run list
Center: Screenshot replay + step timeline + step detail tabs
Right:  Eval Agent controls + observation summary + trace timeline + eval case draft
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

### Header

A thin top bar. The only interactive element is one button:

- **`Reload Sample Runs`** — calls `POST /api/import/molmoweb-sample`, which re-imports the bundled `data/raw/molmoweb_humanskills_sample/` fixtures (idempotent — see [docs/dataset_import.md](dataset_import.md#re-import-behavior)). Disable while in flight; on success, refresh `RunList`. Tooltip: "Re-imports the bundled MolmoWeb-HumanSkills sample. Dataset upload from the browser is a v2 feature." Do not label this button "Import Dataset" — it does not accept user uploads in v1.

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

Renders the agent controls, `ObservationSummaryPanel`, trace timeline, and
follow-up input. The right panel should feel like an AI-native inspection
surface: latest conclusion first, trace available below for audit.

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

Place `ObservationSummaryPanel` immediately below these buttons and above the
chat history so the right panel leads with the current interpretation while the
full trace remains available for audit.

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

### `ObservationSummaryPanel.tsx`

This is the observation-focused summary surface shown in the mock UI. It is a
read model over the latest `AgentTrace` and latest `EvalCase` draft; it does
not create a second analysis path.

- Empty state before analysis: show no findings and keep the follow-up input disabled.
- After analysis: show the latest outcome title, e.g. `Analysis Result (Step 5)`, derived from `EvalCase.failure_step` when a draft exists.
- Show a concise natural-language summary from the latest terminal `propose_eval_case` arguments: `actual_behavior` first, then `expected_behavior` when useful.
- Show `Findings` as `EvalCase.evidence[*].claim`, grouped by `EvidenceItem.source` where useful.
- Show `Suggested Failure Label` from `EvalCase.failure_type`. Do **not** show a model confidence score; if a grounding signal is needed, derive it from evidence structure in a later version.
- Show `Visual Evidence` thumbnails only for evidence items with `run_id` and `step_index` whose source is `trajectory`, `step_detail_high`, or `successful_run`, and where the referenced step has an available screenshot. Clicking a thumbnail selects that step in the center panel.
- Show retrieved context chips for evidence items with `context_id`; clicking opens the matching tool result card in the trace.
- Show unavailable evidence items (`source="unavailable"`) as muted warnings, not as factual proof.
- If the latest turn ended with `budget_exceeded` or `error`, keep any prior summary visible but mark it as stale relative to the latest turn.

### `EvalCaseDraft.tsx`

- Show complete `EvalCase`-shaped draft from the response's `eval_case_draft` field.
- Render every field from the [EvalCase schema](contracts.md#schema-contracts): `failure_step`, `failure_type`, `expected_behavior`, `actual_behavior`, `evidence`, `regression_rule`, `retrieved_context_ids`.
- Render `evidence` as structured rows: claim text, source badge, optional step link, optional trace-event link, and optional retrieved-context link.
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

## Routing

Single-page app with **URL search params**, no router library. Two params:

```text
?run={run_id}             ← selects a run
?run={run_id}&step={i}    ← also selects a step (0-based index)
```

Rules:

- On mount, parse `window.location.search` with `URLSearchParams`. Hydrate `selectedRun` / `selectedStep` from it.
- On selection change, write back with `history.pushState({}, "", "?run=" + id + ...)` so the URL stays in sync.
- Listen to `popstate` so browser back/forward re-applies the URL to component state.
- An unknown `run_id` in the URL renders the empty middle/right panels and shows a "Run not found" inline notice.
- The home URL (`/` with no params) shows the run list with no run selected. Refreshing on any valid URL must restore the exact same view.

Do not add React Router or any routing library for v1 — search params + `history.pushState` covers the requirement in ~50 lines.

## Streaming

`POST /analyze`, `POST /steps/{i}/analyze`, and `POST /followup` return an **NDJSON stream** per [docs/contracts.md](contracts.md#api-contracts). Each line is one JSON object: `{"type": "event", "event": ...}`, `{"type": "done", ...}`, or `{"type": "error", ...}`.

The frontend reads with the standard `fetch` + `ReadableStream` API — no library:

```typescript
async function streamAnalyze(runId: string, onEvent: (e: AgentTraceEvent) => void) {
  const res = await fetch(`/api/runs/${runId}/analyze`, { method: "POST" });
  if (!res.body) throw new Error("no body");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const lines = buf.split("\n");
    buf = lines.pop()!;
    for (const line of lines) {
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === "event") onEvent(msg.event);
      else if (msg.type === "done") return msg; // {eval_case_draft, agent_trace}
      else if (msg.type === "error") throw new Error(msg.error);
    }
  }
}
```

UX rules:

- Append `event` lines to the chat history as they arrive (the agent's tool calls and reasoning appear progressively, not in one drop).
- When the terminal `done` line arrives, reconcile the local trace against `done.agent_trace` (the canonical full state). In practice this should match what was streamed, but the reconciliation step protects against dropped events.
- While the stream is open, the chat input's send button is disabled and the typing indicator is shown.
- On `error` (or fetch-level failure), show an inline error in the chat history and re-enable the input; the user can retry by sending a new follow-up.
- On user navigation away from the run mid-stream, abort the fetch with an `AbortController` and discard the in-flight events.

The same `streamAnalyze` shape is used for `/followup` and the two step-scoped analyze endpoints — only the URL changes. Centralize this helper in `frontend/src/api/stream.ts` and let `EvalAgentPanel` call it.

## State Rules

- A run's trace lifecycle: `none` → `fresh` (first analyze) → `extended` (one or more follow-ups). The `EvalAgentPanel` infers state from `AgentTrace.turn_count`.
- Switching the selected run discards in-flight chat input but does not delete `last_trace.json`. Re-opening the run reloads the persisted trace from `GET /api/runs/{run_id}` (or the dedicated trace endpoint, whichever is wired up).
- Concurrent follow-ups are not supported in v1 — disable the send button while a request is in flight.
- Draft state is **client-only** until the user clicks `Export`. Page refresh = lose unsaved draft edits.
