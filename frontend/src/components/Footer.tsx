import type { TrajectoryRun } from '../types/contracts';

interface FooterProps {
  runs: TrajectoryRun[];
}

export function Footer({ runs }: FooterProps) {
  const total = runs.length;
  const failed = runs.filter(r => r.status === 'failed').length;
  const success = runs.filter(r => r.status === 'success').length;
  const unknown = runs.filter(r => r.status === 'unknown').length;
  const dataset = runs[0]?.source ?? 'MolmoWeb-HumanSkills';
  const latestImport = latestImportTimestamp(runs);

  return (
    <footer className="flex shrink-0 items-center justify-between border-t border-slate-200 bg-white px-5 py-2 text-xs text-slate-500">
      <div className="flex min-w-0 items-center gap-4">
        <span className="truncate">Dataset: <strong className="font-semibold text-slate-700">{dataset}</strong></span>
        {latestImport && <span>Latest import: <strong className="font-semibold text-slate-700">{latestImport}</strong></span>}
      </div>
      <div className="shrink-0">
        {total} runs · <span className="text-red-600">{failed} failed</span> · <span className="text-emerald-600">{success} success</span> · {unknown} unknown
      </div>
    </footer>
  );
}

function latestImportTimestamp(runs: TrajectoryRun[]) {
  const dates = runs
    .map((run) => stringMetadata(run, ['imported_at', 'created_at']))
    .filter((value): value is string => Boolean(value))
    .map((value) => new Date(value))
    .filter((date) => !Number.isNaN(date.getTime()))
    .sort((a, b) => b.getTime() - a.getTime());
  return dates[0]?.toLocaleString() ?? null;
}

function stringMetadata(run: TrajectoryRun, keys: string[]) {
  for (const key of keys) {
    const value = run.metadata[key];
    if (typeof value === 'string' && value) return value;
  }
  return null;
}
