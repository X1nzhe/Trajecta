# Trajecta — Option A redesign code

Drop-in replacements + one targeted patch for the **Option A · Linear polish**
direction picked from `Trajecta Redesign.html`. Hand this folder to Claude
Code; the rules below tell it exactly what to do.

## Files in this folder

| File                                              | Action in `frontend/src/`                                                                   |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `index.css`                                       | **Replace** `frontend/src/index.css`                                                        |
| `actionPalette.ts`                                | **New file** — copy to `frontend/src/components/actionPalette.ts`                          |
| `Header.tsx`                                      | **Replace** `frontend/src/components/Header.tsx`                                            |
| `RunList.tsx`                                     | **Replace** `frontend/src/components/RunList.tsx`                                           |
| `StepTimeline.tsx`                                | **Replace** `frontend/src/components/StepTimeline.tsx`                                      |
| `EvalAgentPanel.ObservationSummary.patch.tsx`     | **Partial patch** — replace ONLY the `ObservationSummaryPanel` function inside the existing `EvalAgentPanel.tsx`. Do not touch any other function in that file. |

## What changes visually

1. **Topbar.** Mono dataset path, inline status counts (`● 3 ● 0 ● 21`), dark
   `Import dataset` CTA. The big indigo "T" logo becomes a slate-900 chip
   to match a sharper, more professional palette.

2. **Sessions list.**
   - IDs render in JetBrains Mono — they read as identifiers, not titles.
   - Each card carries a **mini-trajectory bar** (one colored segment per
     step, color = action type). The shape of a run is visible from the
     list without opening it.
   - Status pill is now `dot + label` (Linear/GitHub style), no border.
   - Filter chips become a tight monospace segmented control (`all/24
     fail/0 ok/3 ?/21`).
   - Selected state is a dark hairline + subtle outer ring instead of a
     tinted background — keeps the cards calm.

3. **Step timeline.** Was numbered dots in tinted circles; now a
   **colored ribbon**. One segment per step, color = action type
   (`click` blue, `scroll` slate, `type` violet, `navigate` green,
   `wait` amber, `unknown` red). Selected segment gets a dark outline.
   Step counter `step 01 / 07` lives top-right in mono. Failure result
   still flags with a red corner dot; "inspected by Eval Agent" steps
   get a small indigo notch beneath the segment.

4. **Eval Agent — Analysis Result section.** Eyebrow labels are smaller
   uppercase + tracked. Evidence list becomes numbered mono indices
   (`01`, `02`, …) instead of grey bullet dots — feels like a trace
   report. Expected-behavior and other sub-sections are now divider-led
   instead of stacked tinted cards.

## Color tokens for action types

Defined once in `actionPalette.ts` and used by both `StepTimeline` (ribbon)
and `RunList` (mini-trajectory):

| Action type | Hex      | Tailwind class    |
| ----------- | -------- | ----------------- |
| `click`     | `#2563eb`| `bg-blue-600`     |
| `type`      | `#7c3aed`| `bg-violet-600`   |
| `scroll`    | `#94a3b8`| `bg-slate-400`    |
| `navigate`  | `#16a34a`| `bg-green-600`    |
| `wait`      | `#f59e0b`| `bg-amber-500`    |
| `unknown`   | `#ef4444`| `bg-red-500`      |

If you have additional action types in production data, extend the maps
in `actionPalette.ts` — both surfaces will pick it up automatically.

## Header API change

`Header` now accepts two optional props:

```ts
<Header onReload={loadRuns} runs={runs} datasetLabel="allenai / MolmoWeb-HumanSkills" />
```

`runs` is used to render the inline `● 3 ● 0 ● 21` count strip; pass
`datasetLabel` to surface the dataset name next to the title. Both are
optional — the header renders cleanly without them. Update the
`<Header />` call in `App.tsx` accordingly.

## Notes for Claude Code

- The existing `App.tsx` background `bg-[#f4f5f8]` should change to
  `bg-[color:var(--color-canvas)]` so it picks up the new warm bone token.
  One-line edit.
- All `border-slate-200` references in remaining files can be migrated to
  `border-[color:var(--color-hairline)]` for visual consistency, but
  this is purely cosmetic — old class still works.
- I did NOT touch the eval-agent streaming / draft / followup logic.
  Every behavior, every prop, every callback is preserved.
- Fonts load from Google Fonts via the `@import` at the top of
  `index.css`. If you self-host fonts, move the import into
  `index.html` and add a `font-display: swap` rule.

## What I did NOT change

- Footer.tsx, ScreenshotViewer.tsx, StepDetailPanel.tsx — leave as-is.
  They'll inherit the new canvas/hairline tokens automatically once
  `index.css` is in.
- Backend, API client, hooks, types — untouched.
- The Eval Agent panel's streaming bubbles, tool-call timeline,
  followup chat, draft modal, trace footer, prompt chips — untouched.
  Only the static `ObservationSummaryPanel` function changes.

## Verification checklist after Claude Code applies this

1. `pnpm dev` / `npm run dev` boots without TypeScript errors.
2. JetBrains Mono loads — IDs in the session list and step counter
   should render in a monospaced font, not Inter.
3. Mini-trajectory bars appear under each session card task text.
4. Step timeline is a row of colored ribbons, not numbered circles.
5. Selecting a session still works (`onSelectRun` wiring intact).
6. Selecting a step still works (`onSelectStep` wiring intact).
7. Eval Agent's "Analysis Result" section renders numbered evidence
   items; no other Eval Agent behavior changes.
