// frontend/src/components/actionPalette.ts
// Shared colors for action types — used by StepTimeline (ribbon) and
// RunList (mini-trajectory). One source of truth so the color of e.g.
// "click" is identical in both surfaces.

import type { StepAction, TrajectoryRun } from '../types/contracts';

export const ACTION_COLOR: Record<StepAction['type'], string> = {
  click:    '#2563eb', // blue-600
  type:     '#7c3aed', // violet-600
  scroll:   '#94a3b8', // slate-400
  navigate: '#16a34a', // green-600
  wait:     '#f59e0b', // amber-500
  unknown:  '#ef4444', // red-500
};

// Tailwind class equivalents — handy where we can use bg-* classes
// (e.g. ribbon segments) instead of inline style for hover state support.
export const ACTION_BG_CLASS: Record<StepAction['type'], string> = {
  click:    'bg-blue-600',
  type:     'bg-violet-600',
  scroll:   'bg-slate-400',
  navigate: 'bg-green-600',
  wait:     'bg-amber-500',
  unknown:  'bg-red-500',
};

// Used by both the run-card mini-trajectory and the main StepTimeline.
// Returns one color per step in run.steps order.
export function actionColors(run: TrajectoryRun): string[] {
  return run.steps.map((step) => ACTION_COLOR[step.action.type] ?? ACTION_COLOR.unknown);
}
