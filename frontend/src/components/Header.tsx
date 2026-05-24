import { useState } from 'react';
import { importDataset } from '../api/client';

interface HeaderProps {
  onReload: () => void;
}

export function Header({ onReload }: HeaderProps) {
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

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b border-slate-200 bg-white px-5 shadow-sm">
      <div className="flex min-w-0 items-center gap-6">
        <div className="flex shrink-0 items-center gap-2">
          <div className="flex h-7 w-7 items-center justify-center rounded-md border border-indigo-200 bg-indigo-50 text-sm font-black text-indigo-700">
            T
          </div>
          <h1 className="text-base font-semibold text-slate-950">Trajecta</h1>
          <span className="rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
            Beta
          </span>
        </div>
      </div>
      <button
        onClick={handleImport}
        disabled={isImporting}
        className="inline-flex items-center gap-2 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 shadow-sm hover:bg-slate-50 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:ring-offset-2 disabled:cursor-wait disabled:opacity-60"
        title="Imports the bundled MolmoWeb-HumanSkills sample. Status badges appear after Eval Agent analysis + human validation."
      >
        <svg className={`h-4 w-4 ${isImporting ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M4 4v5h.5m15.5 6a8 8 0 0 1-14.9 2M20 20v-5h-.5M4 9a8 8 0 0 1 14.9-2" />
        </svg>
        {isImporting ? 'Importing...' : 'Import Dataset'}
      </button>
    </header>
  );
}
