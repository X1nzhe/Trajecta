import { useState } from 'react';
import { truncateTrajectoryId } from '../utils/trajectoryId';

interface TrajectoryIdCopyProps {
  trajectoryId: string;
}

function CopyIcon({ className }: { className?: string }) {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      fill="currentColor"
      className={className}
      aria-hidden
    >
      <path
        fillRule="evenodd"
        d="M4 2a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zm2-1a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1V2a1 1 0 0 0-1-1zM2 5a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1v-1h1v1a2 2 0 0 1-2 2H2a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h1v1z"
      />
    </svg>
  );
}

export function TrajectoryIdCopy({ trajectoryId }: TrajectoryIdCopyProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(trajectoryId);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      console.error('Failed to copy trajectory ID', e);
    }
  };

  return (
    <div className="flex min-w-0 items-center gap-1.5">
      <span
        className="min-w-0 truncate font-mono text-[15px] font-bold text-slate-950"
        title={trajectoryId}
      >
        {truncateTrajectoryId(trajectoryId)}
      </span>
      <button
        type="button"
        onClick={() => void handleCopy()}
        className="shrink-0 rounded p-0.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700"
        aria-label="Copy trajectory ID"
        title={copied ? 'Copied' : 'Copy trajectory ID'}
      >
        {copied ? (
          <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden>
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 13l4 4L19 7" />
          </svg>
        ) : (
          <CopyIcon className="h-4 w-4" />
        )}
      </button>
    </div>
  );
}
