// frontend/src/components/TrajectoryList.tsx — Option A
// Refined session list:
//   - mono IDs (JetBrains Mono) so they read as identifiers
//   - inline mini-trajectory bar on each card (one segment per step)
//   - sharper selected-state (dark hairline + subtle ring, not tinted bg)
//   - status pill is a leading-dot + label, smaller
//   - filter chips are a tight segmented control
//
// Drop-in replacement — same props.

import { useMemo, useState } from 'react';
import type { Trajectory } from '../types/contracts';
import { actionColors } from './actionPalette';

interface TrajectoryListProps {
  trajectories: Trajectory[];
  selectedTrajectoryId: string | null;
  onSelectTrajectory: (trajectoryId: string) => void;
}

export function TrajectoryList({ trajectories, selectedTrajectoryId, onSelectTrajectory }: TrajectoryListProps) {
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState<TrajectoryFilter>('All');

  const counts = useMemo(() => ({
    All: trajectories.length,
    Failed: trajectories.filter((r) => r.status === 'failed').length,
    Success: trajectories.filter((r) => r.status === 'success').length,
    Unverified: trajectories.filter((r) => r.status === 'unknown').length,
  }), [trajectories]);

  const filtered = trajectories.filter((trajectory) => {
    if (search) {
      const q = search.toLowerCase().trim();
      const matches =
        trajectory.task.toLowerCase().includes(q) ||
        trajectory.trajectory_id.toLowerCase().includes(q);
      if (!matches) return false;
    }
    if (filter === 'Failed' && trajectory.status !== 'failed') return false;
    if (filter === 'Success' && trajectory.status !== 'success') return false;
    if (filter === 'Unverified' && trajectory.status !== 'unknown') return false;
    return true;
  });

  return (
    <aside className="flex max-h-[360px] w-full shrink-0 flex-col overflow-hidden rounded-lg border border-[color:var(--color-hairline)] bg-white shadow-sm xl:h-full xl:max-h-none xl:w-[320px]">
      {/* Header */}
      <div className="border-b border-[color:var(--color-hairline)] p-3">
        <div className="mb-3 flex items-center justify-between">
          <div>
            <div className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-slate-500">Sessions</div>
            <div className="mt-0.5 text-[13px] font-bold text-slate-950">
              <span className="tabular-nums">{trajectories.length}</span> trajectories
            </div>
          </div>
          <FilterSegments active={filter} counts={counts} onChange={setFilter} />
        </div>
        <div className="relative">
          <input
            type="text"
            placeholder="Search trajectories…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-9 w-full rounded-md border border-[color:var(--color-hairline)] bg-[color:var(--color-canvas)] pl-8 pr-9 text-sm text-slate-800 placeholder:text-slate-400 focus:border-slate-900 focus:bg-white focus:outline-none focus:ring-0"
          />
          <svg className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="m21 21-4.35-4.35M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Z" />
          </svg>
          <span className="absolute right-2 top-2 rounded border border-[color:var(--color-hairline)] px-1 font-mono text-[10px] text-slate-400">⌘K</span>
        </div>
      </div>

      {/* List */}
      <div className="flex-1 space-y-2 overflow-y-auto bg-[color:var(--color-canvas)] p-2">
        {filtered.map((trajectory) => (
          <TrajectoryCard
            key={trajectory.trajectory_id}
            trajectory={trajectory}
            selected={selectedTrajectoryId === trajectory.trajectory_id}
            onSelect={() => onSelectTrajectory(trajectory.trajectory_id)}
          />
        ))}
        {filtered.length === 0 && (
          <div className="rounded-md border border-dashed border-[color:var(--color-hairline)] bg-white px-3 py-8 text-center text-sm text-slate-500">
            No trajectories found
          </div>
        )}
      </div>
    </aside>
  );
}

function TrajectoryCard({ trajectory, selected, onSelect }: { trajectory: Trajectory; selected: boolean; onSelect: () => void }) {
  const colors = actionColors(trajectory);
  const stepsLabel = `${trajectory.steps.length} ${trajectory.steps.length === 1 ? 'step' : 'steps'}`;
  return (
    <button
      onClick={onSelect}
      className={[
        'group w-full rounded-lg border bg-white p-3 text-left transition-shadow',
        selected
          ? 'border-slate-900 shadow-[0_0_0_3px_rgba(15,23,42,0.05)]'
          : 'border-[color:var(--color-hairline)] hover:border-slate-400',
      ].join(' ')}
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        <span className="min-w-0 truncate font-mono text-[11px] font-semibold text-slate-900" title={trajectory.trajectory_id}>
          {truncateTrajectoryId(trajectory.trajectory_id)}
        </span>
        <StatusPill status={trajectory.status} />
      </div>

      <p className="line-clamp-2 text-[12.5px] leading-5 text-slate-600">
        <span className="text-slate-400">navigate:</span> {trajectory.task}
      </p>

      {/* Mini-trajectory: one tiny bar per step, colored by action type.
         Encodes the shape of the trajectory at a glance. */}
      <div className="mt-3 flex items-center gap-2">
        <div className="flex flex-1 items-center gap-[2px]">
          {colors.slice(0, 28).map((c, i) => (
            <span
              key={i}
              className="h-1 flex-1 rounded-[1px]"
              style={{ background: c, minWidth: 2 }}
            />
          ))}
          {colors.length > 28 && (
            <span className="ml-1 font-mono text-[9.5px] text-slate-400">+{colors.length - 28}</span>
          )}
        </div>
        <div className="flex items-center gap-2 font-mono text-[10px] tabular-nums text-slate-500">
          <span>{stepsLabel}</span>
          {commentCount(trajectory) > 0 && (
            <span className="inline-flex items-center gap-1" title="Local review comments">
              <svg className="h-3 w-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7A8.4 8.4 0 0 1 4 11.5 8.5 8.5 0 0 1 12.5 3 8.5 8.5 0 0 1 21 11.5Z" />
              </svg>
              {commentCount(trajectory)}
            </span>
          )}
        </div>
      </div>

      <div className="mt-1 font-mono text-[10px] text-slate-400">{formatTrajectoryDate(trajectory)}</div>
    </button>
  );
}

function StatusPill({ status }: { status: Trajectory['status'] }) {
  const map = {
    failed:  { dot: '#dc2626', c: 'text-red-700',     bg: 'bg-red-50',     label: 'Failed'     },
    success: { dot: '#16a34a', c: 'text-emerald-700', bg: 'bg-emerald-50', label: 'Success'    },
    unknown: { dot: '#d97706', c: 'text-amber-700',   bg: 'bg-amber-50',   label: 'Unverified' },
  }[status];
  return (
    <span className={`inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] font-semibold tracking-wide ${map.bg} ${map.c}`}>
      <span className="h-1.5 w-1.5 rounded-full" style={{ background: map.dot }} />
      {map.label}
    </span>
  );
}

type TrajectoryFilter = 'All' | 'Failed' | 'Success' | 'Unverified';

// Short label per filter. Counts live in the hover title so the rail
// stays compact — the "all/24 fail/0" form was loud at small panel widths.
const FILTER_SHORT_LABEL: Record<TrajectoryFilter, string> = {
  All: 'all',
  Failed: 'fail',
  Success: 'ok',
  Unverified: '?',
};

function FilterSegments({
  active,
  counts,
  onChange,
}: {
  active: TrajectoryFilter;
  counts: Record<TrajectoryFilter, number>;
  onChange: (f: TrajectoryFilter) => void;
}) {
  const items: TrajectoryFilter[] = ['All', 'Failed', 'Success', 'Unverified'];
  return (
    <div className="flex items-center gap-0.5 font-mono text-[10px]">
      {items.map((f) => {
        const isActive = active === f;
        return (
          <button
            key={f}
            onClick={() => onChange(f)}
            className={[
              'rounded px-1.5 py-0.5 transition-colors',
              isActive ? 'bg-slate-900 text-white' : 'text-slate-500 hover:text-slate-900',
            ].join(' ')}
            title={`${f} (${counts[f]})`}
          >
            {FILTER_SHORT_LABEL[f]}
          </button>
        );
      })}
    </div>
  );
}

function truncateTrajectoryId(id: string) {
  if (id.length <= 14) return id;
  return `${id.slice(0, 8)}…${id.slice(-4)}`;
}

function formatTrajectoryDate(trajectory: Trajectory) {
  const raw = firstString(trajectory.metadata, ['created_at', 'imported_at', 'date']) ?? trajectory.steps[0]?.timestamp;
  if (!raw) return 'Date unavailable';
  const numeric = Number(raw);
  const date = Number.isFinite(numeric)
    ? new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000)
    : new Date(raw);
  if (Number.isNaN(date.getTime())) return 'Date unavailable';
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function commentCount(trajectory: Trajectory) {
  const value = trajectory.metadata.comment_count ?? trajectory.metadata.comments_count ?? trajectory.metadata.review_comment_count;
  return typeof value === 'number' ? value : 0;
}

function firstString(metadata: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const v = metadata[key];
    if (typeof v === 'string' && v) return v;
  }
  return null;
}
