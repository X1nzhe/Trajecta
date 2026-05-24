import type { TrajectoryRun } from '../types/contracts';

interface StepTimelineProps {
  run: TrajectoryRun;
  selectedStepIndex: number;
  inspectedSteps?: Set<number>;
  onSelectStep: (index: number) => void;
}

export function StepTimeline({ run, selectedStepIndex, inspectedSteps = new Set(), onSelectStep }: StepTimelineProps) {
  return (
    <div className="overflow-x-auto border-b border-slate-200 bg-white px-3 py-1.5">
      <div className="flex min-w-max items-start">
        {run.steps.map((step, i) => {
          const stepIndex = step.index ?? i;
          const isSelected = selectedStepIndex === stepIndex;
          const inspected = inspectedSteps.has(stepIndex);
          const statusClasses = stepStatusClasses(step.result.status, isSelected);
          const action = step.action.type || 'unknown';
          return (
            <div key={stepIndex} className="flex items-start">
              <button
                onClick={() => onSelectStep(stepIndex)}
                className="group flex w-10 flex-col items-center gap-0.5 text-center"
                title={`Step ${stepIndex + 1}: ${action}${inspected ? ' - inspected by Eval Agent' : ''}`}
              >
                <span className={`relative flex h-6 w-6 items-center justify-center rounded-full border text-[11px] font-bold transition-all ${statusClasses}`}>
                  {stepIndex + 1}
                  {inspected && (
                    <span className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full border border-white bg-indigo-500" />
                  )}
                </span>
                <span className={`max-w-[38px] truncate text-[9px] font-medium leading-3 ${isSelected ? 'text-slate-900' : 'text-slate-500 group-hover:text-slate-700'}`}>
                  {action}
                </span>
              </button>
              {i < run.steps.length - 1 && <div className="mt-3 h-px w-5 bg-slate-200" />}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function stepStatusClasses(status: TrajectoryRun['steps'][number]['result']['status'], isSelected: boolean) {
  if (isSelected) {
    if (status === 'failed') return 'border-red-500 bg-red-500 text-white shadow-sm ring-4 ring-red-50';
    if (status === 'success') return 'border-emerald-500 bg-emerald-500 text-white shadow-sm ring-4 ring-emerald-50';
    return 'border-indigo-500 bg-indigo-600 text-white shadow-sm ring-4 ring-indigo-50';
  }

  if (status === 'failed') return 'border-red-200 bg-red-50 text-red-700 hover:border-red-300';
  if (status === 'success') return 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:border-emerald-300';
  return 'border-slate-200 bg-slate-100 text-slate-500 hover:border-slate-300';
}
