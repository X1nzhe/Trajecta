# Frontend UI

Do not overbuild.

Single-page app is enough.

Layout:

```text
Left: Run list
Center: Screenshot replay + step timeline
Right: Eval Agent panel
Bottom or side: Eval case draft
```

## Components

`RunList.tsx`

- list runs
- show status
- click to load run

`StepTimeline.tsx`

- show step numbers
- show action type
- select step

`ScreenshotViewer.tsx`

- show selected step screenshot
- load screenshots from `/api/runs/{run_id}/screenshots/{filename}` or an API-provided screenshot URL
- draw coordinate marker only if coordinate validation is `validated`
- draw bbox only when bbox exists, the screenshot is available, and bbox bounds are validated

`StepDetailPanel.tsx`

- action
- observation
- result
- coordinate validation
- metadata

`EvalAgentPanel.tsx`

- button: Analyze Run
- button: Analyze Selected Step
- render the agent's tool-call trace (ordered list of tool calls + results), so the user can see what the agent looked at and why
- highlight which steps the agent inspected via `get_step_detail` (clicking jumps the `StepTimeline` to that step)
- show retrieved failure-memory and eval-case results
- show termination reason: `propose_eval_case`, `budget_exceeded`, or `error`
- for `error`, show the latest `tool_error` or `AgentTraceEvent.error` message
- pass generated eval case draft to `EvalCaseDraft.tsx`

`EvalCaseDraft.tsx`

- show complete `EvalCase`-shaped draft
- render key fields from the `EvalCase` contract
- allow user to review or edit draft fields before export
- include a `Mark validated` checkbox or toggle that explicitly sets `human_validated=true`
- disable export while `human_validated=false`
- button: Export Eval Case

## UI Copy

Use product wording:

```text
Raw trajectory -> AI-assisted analysis -> Human validation -> Regression eval case
```

Avoid saying:

```text
chatbot
browser automation agent
generic observability platform
```

This is an eval-case authoring tool.
