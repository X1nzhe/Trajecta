import type { TrajectoryStep } from '../types';

type Props = {
  runId: string;
  step: TrajectoryStep | null;
};

export function ScreenshotViewer({ runId, step }: Props) {
  if (!step) return <div><h3>Screenshot</h3><p>Select a step.</p></div>;

  const hasCoordinates =
    step.coordinates &&
    Number.isFinite(step.coordinates.x) &&
    Number.isFinite(step.coordinates.y);

  return (
    <div>
      <h3>Screenshot</h3>
      <div style={{ border: '1px solid #ddd', padding: 12, minHeight: 120, position: 'relative' }}>
        <p><strong>Run:</strong> {runId}</p>
        <p><strong>Image:</strong> {step.screenshot_path}</p>
        {hasCoordinates && (
          <p>
            <strong>Overlay:</strong> ({step.coordinates!.x}, {step.coordinates!.y})
          </p>
        )}
      </div>
    </div>
  );
}
