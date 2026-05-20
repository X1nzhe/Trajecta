import type { TrajectoryStep } from '../types';

type Props = {
  step: TrajectoryStep | null;
};

export function StepDetailPanel({ step }: Props) {
  return (
    <div>
      <h3>Step Detail</h3>
      {!step ? (
        <p>No step selected.</p>
      ) : (
        <ul>
          <li><strong>Action:</strong> {step.action}</li>
          <li><strong>Target:</strong> {step.target || 'n/a'}</li>
          <li><strong>Status:</strong> {step.success ? 'success' : 'failed'}</li>
          <li><strong>Error:</strong> {step.error || 'n/a'}</li>
        </ul>
      )}
    </div>
  );
}
