import type { TrajectoryStep } from '../types';

type Props = {
  steps: TrajectoryStep[];
  selectedStepId: string | null;
  onSelectStep: (stepId: string) => void;
};

export function StepTimeline({ steps, selectedStepId, onSelectStep }: Props) {
  return (
    <div>
      <h3>Step Timeline</h3>
      <ol>
        {steps.map((step) => (
          <li key={step.step_id}>
            <button
              type="button"
              style={{ fontWeight: selectedStepId === step.step_id ? 700 : 400 }}
              onClick={() => onSelectStep(step.step_id)}
            >
              {step.step_id}: {step.action} {step.success ? '✅' : '❌'}
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}
