// frontend/src/components/Header.tsx — Option A
// Refined topbar: mono IDs, dataset name surfaced, status counts inline.
// Same API as before (onReload prop) — drop-in replacement.

import { useState } from 'react';
import { importDataset } from '../api/client';
import type { Trajectory } from '../types/contracts';

interface HeaderProps {
  onReload: () => void;
  trajectories?: Trajectory[];      // optional — shows status counts in the bar
  datasetLabel?: string;        // e.g. "allenai / MolmoWeb-HumanSkills"
}

export function Header({ onReload, trajectories = [], datasetLabel }: HeaderProps) {
  const [isImporting, setIsImporting] = useState(false);

  const handleImport = async () => {
    setIsImporting(true);
    try {
      await importDataset();
      onReload();
    } catch (e) {
      console.error(e);
      alert('Failed to import dataset');
    } finally {
      setIsImporting(false);
    }
  };

  const counts = {
    success: trajectories.filter((r) => r.status === 'success').length,
    failed: trajectories.filter((r) => r.status === 'failed').length,
    unknown: trajectories.filter((r) => r.status === 'unknown').length,
  };

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-[color:var(--color-hairline)] bg-white px-5">
      <div className="flex min-w-0 items-center gap-3">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-slate-900 text-[13px] font-extrabold tracking-tight text-white">
          T
        </div>
        <div className="flex items-baseline gap-2">
          <h1 className="text-[15px] font-bold tracking-tight text-slate-950">Trajecta</h1>
          <span className="rounded border border-slate-200 px-1.5 py-px font-mono text-[10px] text-slate-500">
            v0.4 · beta
          </span>
        </div>
        {datasetLabel && (
          <>
            <span className="mx-1 h-4 w-px bg-slate-200" aria-hidden="true" />
            <span className="truncate font-mono text-[11.5px] text-slate-500">{datasetLabel}</span>
          </>
        )}
      </div>

      <div className="flex items-center gap-4">
        {trajectories.length > 0 && (
          <div className="hidden items-center gap-3 text-[11.5px] text-slate-500 md:flex">
            <Dot color="#16a34a" /> <span className="tabular-nums">{counts.success}</span>
            <Dot color="#dc2626" /> <span className="tabular-nums">{counts.failed}</span>
            <Dot color="#d97706" /> <span className="tabular-nums">{counts.unknown}</span>
            <span className="font-mono text-slate-400">· {trajectories.length} total</span>
          </div>
        )}
        <button
          onClick={handleImport}
          disabled={isImporting}
          className="inline-flex items-center gap-1.5 rounded-md bg-slate-900 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-slate-800 disabled:cursor-wait disabled:opacity-60"
          title="Imports the bundled MolmoWeb-HumanSkills sample. Status badges appear after Eval Agent analysis + human validation."
        >
          <svg className={`h-3.5 w-3.5 ${isImporting ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M4 4v5h.5m15.5 6a8 8 0 0 1-14.9 2M20 20v-5h-.5M4 9a8 8 0 0 1 14.9-2" />
          </svg>
          {isImporting ? 'Importing…' : 'Import dataset'}
        </button>
      </div>
    </header>
  );
}

function Dot({ color }: { color: string }) {
  return <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: color }} />;
}
