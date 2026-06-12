// frontend/src/components/StepTimeline.tsx — Option A
// Step timeline as a color-coded RIBBON, not numbered dots.
// Each step is a colored segment (color = action type); the selected
// step is highlighted with a dark outline. Failure result still shows
// the red dot in the corner. Inspected-by-eval-agent steps get a small
// indigo notch beneath the segment.
//
// Drop-in replacement — same props.

import { useRef } from 'react';
import type { Trajectory } from '../types/contracts';
import { ACTION_COLOR } from './actionPalette';

interface StepTimelineProps {
  trajectory: Trajectory;
  selectedStepIndex: number;
  inspectedSteps?: Set<number>;
  onSelectStep: (index: number) => void;
}

export function StepTimeline({ trajectory, selectedStepIndex, inspectedSteps = new Set(), onSelectStep }: StepTimelineProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);

  // Translate vertical wheel to horizontal scroll on the step rail.
  // Browsers do this automatically only with Shift held; without it
  // the wheel either does nothing (no vertical overflow here) or
  // bubbles up to scroll the page, which felt broken on a clearly
  // horizontal rail.
  const handleWheel = (e: React.WheelEvent<HTMLDivElement>) => {
    const el = scrollerRef.current;
    if (!el) return;
    const dx = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    if (dx === 0) return;
    const maxScroll = el.scrollWidth - el.clientWidth;
    if (maxScroll <= 0) return;
    // Only consume the event if the rail can actually move in that
    // direction — otherwise let the page scroll normally.
    const atStart = el.scrollLeft <= 0 && dx < 0;
    const atEnd = el.scrollLeft >= maxScroll && dx > 0;
    if (atStart || atEnd) return;
    e.preventDefault();
    el.scrollLeft += dx;
  };

  return (
    <div className="border-b border-[color:var(--color-hairline)] bg-white px-4 py-3">
      <div className="mb-1 flex items-center justify-between">
        <div className="text-[10.5px] font-semibold uppercase tracking-[0.1em] text-slate-500">
          Trajectory · {trajectory.steps.length} {trajectory.steps.length === 1 ? 'step' : 'steps'}
        </div>
        <div className="font-mono text-[11px] text-slate-500">
          step{' '}
          <span className="font-semibold text-slate-900 tabular-nums">
            {pad2(selectedStepIndex)}
          </span>{' '}
          / <span className="tabular-nums">{pad2(trajectory.steps.length)}</span>
        </div>
      </div>

      <div
        ref={scrollerRef}
        onWheel={handleWheel}
        className="scrollbar-thin flex items-stretch gap-1 overflow-x-auto pt-1 pb-1.5"
      >
        {trajectory.steps.map((step, i) => {
          const idx = step.index ?? i + 1;
          const isSelected = selectedStepIndex === idx;
          const isFailure = step.result.status === 'failed';
          const inspected = inspectedSteps.has(idx);
          const baseColor = ACTION_COLOR[step.action.type] ?? ACTION_COLOR.unknown;
          return (
            <button
              key={idx}
              onClick={() => onSelectStep(idx)}
              className="group relative flex flex-1 min-w-[28px] flex-col items-center"
              title={`Step ${idx}: ${step.action.type}${inspected ? ' · inspected by Eval Agent' : ''}`}
            >
              {/* Segment */}
              <span
                className={`block h-6 w-full rounded transition-opacity ${isSelected ? '' : 'opacity-45 group-hover:opacity-80'}`}
                style={{
                  background: baseColor,
                  outline: isSelected ? '2px solid var(--color-ink)' : 'none',
                  outlineOffset: 1,
                }}
              >
                {isFailure && (
                  <span
                    className="absolute right-0 top-0 h-2 w-2 -translate-y-0.5 translate-x-0.5 rounded-full border-[1.5px] border-white"
                    style={{ background: '#ef4444' }}
                    aria-hidden="true"
                  />
                )}
              </span>

              {/* Index + action label */}
              <span className={`mt-1.5 font-mono text-[10px] tabular-nums ${isSelected ? 'font-semibold text-slate-900' : 'text-slate-500'}`}>
                {pad2(idx)}
              </span>
              <span className="truncate text-[9.5px] leading-tight text-slate-400">
                {step.action.type}
              </span>

              {/* Inspected notch */}
              {inspected && (
                <span
                  className="absolute -bottom-0.5 left-1/2 h-0.5 w-3 -translate-x-1/2 rounded-full bg-indigo-500"
                  aria-hidden="true"
                />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function pad2(n: number) {
  return n < 10 ? `0${n}` : String(n);
}
