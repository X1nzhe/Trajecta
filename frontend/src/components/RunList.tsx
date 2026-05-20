import type { TrajectoryRun } from '../types';

type Props = {
  runs: TrajectoryRun[];
  selectedRunId: string | null;
  onSelect: (runId: string) => void;
};

export function RunList({ runs, selectedRunId, onSelect }: Props) {
  return (
    <div>
      <h3>Runs</h3>
      <ul>
        {runs.map((run) => (
          <li key={run.run_id}>
            <button
              type="button"
              style={{ fontWeight: selectedRunId === run.run_id ? 700 : 400 }}
              onClick={() => onSelect(run.run_id)}
            >
              {run.run_id}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
