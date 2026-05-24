import { useState } from 'react';
import type { TrajectoryStep } from '../types/contracts';

interface StepDetailPanelProps {
  step: TrajectoryStep;
  isExpanded: boolean;
  onExpandedChange: (isExpanded: boolean) => void;
}

type TabId = 'action' | 'observation' | 'coord' | 'meta';

export function StepDetailPanel({ step, isExpanded, onExpandedChange }: StepDetailPanelProps) {
  const [activeTab, setActiveTab] = useState<TabId>('action');

  const tabs = [
    { id: 'action', label: 'Action' },
    { id: 'observation', label: 'Observation' },
    { id: 'coord', label: 'Coordinate Validation' },
    { id: 'meta', label: 'Metadata' },
  ] as const;

  const handleTabClick = (tabId: TabId) => {
    setActiveTab(tabId);
    if (!isExpanded) onExpandedChange(true);
  };

  return (
    <section className="mx-3 mb-3 mt-auto shrink-0 overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className={`${isExpanded ? 'border-b border-slate-200' : ''} flex min-h-10 items-center gap-2 px-3`}>
        <div className="flex min-w-0 flex-1 overflow-hidden">
          {tabs.map(tab => (
            <button
              key={tab.id}
              onClick={() => handleTabClick(tab.id)}
              className={`mb-[-1px] shrink-0 border-b-2 px-3 py-2 text-xs font-semibold transition-colors ${
                activeTab === tab.id
                  ? 'border-indigo-600 text-indigo-700'
                  : 'border-transparent text-slate-500 hover:border-slate-300 hover:text-slate-700'
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
        <button
          onClick={() => onExpandedChange(!isExpanded)}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-slate-200 bg-white text-slate-500 hover:bg-slate-50 hover:text-indigo-700"
          title={isExpanded ? 'Hide step details' : 'Show step details'}
        >
          {isExpanded ? <ChevronDownIcon /> : <ChevronUpIcon />}
        </button>
      </div>

      {isExpanded && (
        <div className="overflow-hidden bg-white p-3 text-xs text-slate-700">
          {activeTab === 'action' && (
            <div className="grid grid-cols-[104px_minmax(0,1fr)_104px_minmax(0,1fr)] gap-x-3 gap-y-2">
              <DetailLabel>Action Type</DetailLabel>
              <div><CodePill>{step.action.type}</CodePill></div>

              <DetailLabel>Coordinates</DetailLabel>
              <CodeBlock>{step.action.coordinates ? `(${step.action.coordinates.x}, ${step.action.coordinates.y})` : '-'}</CodeBlock>

              <DetailLabel>Label</DetailLabel>
              <div className="truncate" title={step.action.label || '-'}>{step.action.label || '-'}</div>

              <DetailLabel>Text</DetailLabel>
              <div className="truncate" title={step.action.text || '-'}>{step.action.text || '-'}</div>

              <DetailLabel>Element Selector</DetailLabel>
              <CodeBlock>{metadataString(step, ['selector', 'element_selector', 'css_selector'])}</CodeBlock>

              <DetailLabel>Bounding Box</DetailLabel>
              <CodeBlock>{step.action.bbox ? `[${step.action.bbox.x}, ${step.action.bbox.y}, ${step.action.bbox.width}, ${step.action.bbox.height}]` : '-'}</CodeBlock>

              <DetailLabel>Raw</DetailLabel>
              <CodeBlock className="col-span-3">{step.action.raw || '-'}</CodeBlock>
            </div>
          )}

          {activeTab === 'observation' && (
            <div className="grid grid-cols-[104px_minmax(0,1fr)_104px_minmax(0,1fr)] gap-x-3 gap-y-2">
              <DetailLabel>URL</DetailLabel>
              <div className="col-span-3 truncate text-blue-600" title={step.observation.url || '-'}>
                <a href={step.observation.url} target="_blank" rel="noreferrer">{step.observation.url || '-'}</a>
              </div>
                
              <DetailLabel>Title</DetailLabel>
              <div className="truncate" title={step.observation.title || '-'}>{step.observation.title || '-'}</div>
                
              <DetailLabel>Visible Text</DetailLabel>
              <CodeBlock>{step.observation.visible_text || '-'}</CodeBlock>

              <DetailLabel>Visual Evidence</DetailLabel>
              <div className="col-span-3 truncate" title={step.observation.visual_evidence.join(' | ') || '-'}>
                {step.observation.visual_evidence.length > 0 ? (
                  step.observation.visual_evidence.join(' | ')
                ) : '-'}
              </div>
            </div>
          )}

          {activeTab === 'coord' && (
            <div className="grid grid-cols-[104px_minmax(0,1fr)_104px_minmax(0,1fr)] gap-x-3 gap-y-2">
              <DetailLabel>Status</DetailLabel>
              <div>
                <span className={`rounded px-2 py-1 text-xs font-medium ${
                  step.coordinate_validation.status === 'validated' ? 'bg-green-100 text-green-700' :
                  step.coordinate_validation.status === 'out_of_bounds' ? 'bg-red-100 text-red-700' :
                  'bg-gray-200 text-gray-700'
                }`}>
                  {step.coordinate_validation.status}
                </span>
              </div>
                
              <DetailLabel>Image Size</DetailLabel>
              <div className="font-mono">{step.coordinate_validation.image_width} x {step.coordinate_validation.image_height}</div>
                
              <DetailLabel>Reason</DetailLabel>
              <div className="col-span-3 truncate" title={step.coordinate_validation.reason || '-'}>{step.coordinate_validation.reason || '-'}</div>
            </div>
          )}

          {activeTab === 'meta' && (
            <div className="grid grid-cols-[104px_minmax(0,1fr)] gap-x-3 gap-y-2">
              <DetailLabel>Timestamp</DetailLabel>
              <div className="font-mono">{step.timestamp || '-'}</div>
                
              <DetailLabel>Metadata</DetailLabel>
              <pre className="truncate rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-mono text-xs" title={JSON.stringify(step.metadata, null, 2)}>{JSON.stringify(step.metadata, null, 2)}</pre>
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function ChevronUpIcon() {
  return (
    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="m6 15 6-6 6 6" />
    </svg>
  );
}

function ChevronDownIcon() {
  return (
    <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="m6 9 6 6 6-6" />
    </svg>
  );
}

function DetailLabel({ children }: { children: string }) {
  return <div className="pt-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">{children}</div>;
}

function CodePill({ children }: { children: string }) {
  return <span className="inline-flex rounded-md border border-slate-200 bg-slate-50 px-2 py-0.5 font-mono text-xs text-slate-800">{children}</span>;
}

function CodeBlock({ children, className = '' }: { children: string; className?: string }) {
  return (
    <div
      className={`min-w-0 truncate rounded-md border border-slate-200 bg-slate-50 px-2 py-1 font-mono text-xs text-slate-800 ${className}`}
      title={children}
    >
      {children}
    </div>
  );
}

function metadataString(step: TrajectoryStep, keys: string[]) {
  for (const key of keys) {
    const value = step.metadata[key];
    if (typeof value === 'string' && value) return value;
  }
  return '-';
}
