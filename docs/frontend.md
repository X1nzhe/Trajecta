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
- draw coordinate marker only if coordinate validation is `validated`
- draw bbox if bbox exists

`StepDetailPanel.tsx`

- action
- observation
- result
- coordinate validation
- metadata

`EvalAgentPanel.tsx`

- button: Analyze Run
- button: Analyze Selected Step
- show suggested failure type
- show evidence
- show similar retrieved cases
- show eval case draft
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
