import { useMemo, useState } from 'react';
import type { TrajectoryRun } from '../types/contracts';

interface RunListProps {
  runs: TrajectoryRun[];
  selectedRunId: string | null;
  onSelectRun: (runId: string) => void;
}

export function RunList({ runs, selectedRunId, onSelectRun }: RunListProps) {
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<RunFilter>('All');

  const counts = useMemo(() => ({
    All: runs.length,
    Failed: runs.filter((run) => run.status === 'failed').length,
    Success: runs.filter((run) => run.status === 'success').length,
    Unverified: runs.filter((run) => run.status === 'unknown').length,
  }), [runs]);

  const filteredRuns = runs.filter((run) => {
    if (search && !run.task.toLowerCase().includes(search.toLowerCase())) return false;
    if (filter === 'Failed' && run.status !== 'failed') return false;
    if (filter === 'Success' && run.status !== 'success') return false;
    if (filter === 'Unverified' && run.status !== 'unknown') return false;
    return true;
  });

  return (
    <aside className="flex max-h-[360px] w-full shrink-0 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm xl:h-full xl:max-h-none xl:w-[320px]">
      <div className="border-b border-slate-200 p-3">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[11px] font-bold uppercase tracking-[0.12em] text-slate-600">Sessions</h2>
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500">
            {runs.length} total
          </span>
        </div>
        <div className="relative mb-3">
          <input
            type="text"
            placeholder="Search trajectories..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-10 w-full rounded-md border border-slate-200 bg-white pl-9 pr-9 text-sm text-slate-800 placeholder:text-slate-400 focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500"
          />
          <svg className="absolute left-3 top-3 h-4 w-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="m21 21-4.35-4.35M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Z" />
          </svg>
          <svg className="absolute right-3 top-3 h-4 w-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M4 6h16M7 12h10m-7 6h4" />
          </svg>
        </div>
        <div className="flex items-center gap-2 text-xs">
          {filters.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`rounded-full px-2.5 py-1 font-medium transition-colors ${
                filter === f
                  ? activeFilterClass(f)
                  : 'bg-white text-slate-600 hover:bg-slate-50'
              }`}
            >
              {f} <span className={filter === f ? 'opacity-80' : 'text-slate-400'}>{counts[f]}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="flex-1 space-y-2 overflow-y-auto bg-slate-50/70 p-2">
        {filteredRuns.map((run) => (
          <button
            key={run.run_id}
            onClick={() => onSelectRun(run.run_id)}
            className={`group relative w-full rounded-lg border bg-white p-3 text-left shadow-sm transition-colors hover:border-indigo-200 ${
              selectedRunId === run.run_id ? selectedRunClass(run.status) : 'border-slate-200'
            }`}
          >
            <div className="mb-2 flex items-start justify-between gap-3">
              <span className="min-w-0 truncate text-sm font-bold text-slate-950" title={run.run_id}>
                Trajectory {truncateRunId(run.run_id)}
              </span>
              <StatusBadge status={run.status} />
            </div>
            <p className="mb-4 line-clamp-2 text-sm leading-5 text-slate-600">{run.task}</p>
            <div className="flex items-center justify-between text-xs text-slate-500">
              <span>{formatRunDate(run)}</span>
              <div className="flex items-center gap-3">
                <span>{run.steps.length} steps</span>
                <span className="inline-flex items-center gap-1" title="Local review comments">
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7A8.4 8.4 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5Z" />
                  </svg>
                  {commentCount(run)}
                </span>
              </div>
            </div>
          </button>
        ))}
        {filteredRuns.length === 0 && (
          <div className="rounded-lg border border-dashed border-slate-200 bg-white px-3 py-8 text-center text-sm text-slate-500">
            No trajectories found
          </div>
        )}
      </div>
    </aside>
  );
}

type RunFilter = 'All' | 'Failed' | 'Success' | 'Unverified';

const filters: RunFilter[] = ['All', 'Failed', 'Success', 'Unverified'];

function StatusBadge({ status }: { status: TrajectoryRun['status'] }) {
  const classes = {
    failed: 'bg-red-50 text-red-700 border-red-100',
    success: 'bg-emerald-50 text-emerald-700 border-emerald-100',
    unknown: 'bg-amber-50 text-amber-700 border-amber-100',
  }[status];
  const label = status === 'failed' ? 'Failed' : status === 'success' ? 'Success' : 'Unverified';
  return <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${classes}`}>{label}</span>;
}

function activeFilterClass(filter: RunFilter) {
  if (filter === 'Failed') return 'bg-red-50 text-red-700';
  if (filter === 'Success') return 'bg-emerald-50 text-emerald-700';
  if (filter === 'Unverified') return 'bg-amber-50 text-amber-700';
  return 'bg-slate-900 text-white';
}

function selectedRunClass(status: TrajectoryRun['status']) {
  if (status === 'failed') return 'border-red-300 bg-red-50/40 ring-1 ring-red-200';
  if (status === 'success') return 'border-emerald-300 bg-emerald-50/40 ring-1 ring-emerald-200';
  return 'border-amber-300 bg-amber-50/40 ring-1 ring-amber-200';
}

function truncateRunId(id: string) {
  if (id.length <= 12) return id;
  return `${id.slice(0, 8)}...${id.slice(-4)}`;
}

function formatRunDate(run: TrajectoryRun) {
  const raw = firstString(run.metadata, ['created_at', 'imported_at', 'date']) ?? run.steps[0]?.timestamp;
  if (!raw) return 'Date unavailable';
  const numeric = Number(raw);
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000)
    : new Date(raw);
  if (Number.isNaN(date.getTime())) return 'Date unavailable';
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function commentCount(run: TrajectoryRun) {
  const value = run.metadata.comment_count ?? run.metadata.comments_count ?? run.metadata.review_comment_count;
  return typeof value === 'number' ? value : 0;
}

function firstString(metadata: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = metadata[key];
    if (typeof value === 'string' && value) return value;
  }
  return null;
}
