import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import { createEvalCase } from '../api/client';
import { streamAgentRequest } from '../api/stream';
import type { AgentTrace, AgentTraceEvent, EvalCase, EvidenceItem, TrajectoryRun } from '../types/contracts';

interface EvalAgentPanelProps {
  run: TrajectoryRun | null;
  selectedStepIndex: number | null;
  trace: AgentTrace | null;
  evalCaseDraft: EvalCase | null;
  onTraceChange: Dispatch<SetStateAction<AgentTrace | null>>;
  onDraftChange: Dispatch<SetStateAction<EvalCase | null>>;
  onSelectStep: (index: number) => void;
  onEvalCaseValidated?: () => void;
}

type AnalyzeMode = 'run' | 'step';

export function EvalAgentPanel({
  run,
  selectedStepIndex,
  trace,
  evalCaseDraft,
  onTraceChange,
  onDraftChange,
  onSelectStep,
  onEvalCaseValidated,
}: EvalAgentPanelProps) {
  const [input, setInput] = useState('');
  const [inFlight, setInFlight] = useState(false);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [pendingUserMessage, setPendingUserMessage] = useState<string | null>(null);
  const [expandedEvents, setExpandedEvents] = useState<Set<number>>(new Set());
  const [feedback, setFeedback] = useState<'up' | 'down' | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const latestToolError = useMemo(() => latestError(trace), [trace]);
  const hasTrace = Boolean(trace && trace.turn_count > 0);

  useEffect(() => {
    abortRef.current?.abort();
    setInput('');
    setPanelError(null);
    setPendingUserMessage(null);
    setExpandedEvents(new Set());
    setFeedback(null);
  }, [run?.run_id]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const runAnalysis = async (mode: AnalyzeMode) => {
    if (!run || inFlight) return;
    if (mode === 'step' && selectedStepIndex === null) return;

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setInFlight(true);
    setPanelError(null);
    setPendingUserMessage(null);
    onDraftChange(null);
    onTraceChange(emptyTrace(run.run_id, mode, selectedStepIndex));

    const url = mode === 'run'
      ? `/api/runs/${run.run_id}/analyze`
      : `/api/runs/${run.run_id}/steps/${selectedStepIndex}/analyze`;

    try {
      const done = await streamAgentRequest(url, {
        signal: controller.signal,
        onEvent: (event) => onTraceChange(appendEvent(run.run_id, mode, selectedStepIndex, event)),
      });
      onTraceChange(done.agent_trace);
      onDraftChange(done.eval_case_draft);
    } catch (error) {
      if (!isAbortError(error)) setPanelError(errorMessage(error));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setInFlight(false);
    }
  };

  const sendFollowup = async () => {
    if (!run || !trace || inFlight) return;
    const message = input.trim();
    if (!message) return;

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setInFlight(true);
    setPanelError(null);
    setPendingUserMessage(message);
    setInput('');

    try {
      const done = await streamAgentRequest(`/api/runs/${run.run_id}/followup`, {
        body: { message },
        signal: controller.signal,
        onEvent: (event) => {
          setPendingUserMessage(null);
          onTraceChange(appendEvent(run.run_id, trace.user_intent === 'analyze_step' ? 'step' : 'run', trace.selected_step ?? null, event));
        },
      });
      onTraceChange(done.agent_trace);
      if (done.eval_case_draft) onDraftChange(done.eval_case_draft);
    } catch (error) {
      if (!isAbortError(error)) setPanelError(errorMessage(error));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setPendingUserMessage(null);
      setInFlight(false);
    }
  };

  const rerunLatest = () => {
    if (!run || inFlight) return;
    const shouldRerun = window.confirm('Start a fresh analysis for this run? The current trace view will be replaced.');
    if (!shouldRerun) return;
    if (trace?.user_intent === 'analyze_step') {
      runAnalysis('step');
    } else {
      runAnalysis('run');
    }
  };

  const chipTemplates = promptChips(selectedStepIndex);
  const inputDisabled = !run || !hasTrace || inFlight;

  return (
    <aside className="flex max-h-[680px] w-full shrink-0 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm xl:h-full xl:max-h-none xl:w-[410px]">
      <div className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-indigo-50 text-indigo-700">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M13 3 4 14h7l-1 7 9-11h-7l1-7Z" />
            </svg>
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="font-bold text-slate-950">Eval Agent</h2>
              <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">Beta</span>
            </div>
            <TerminationBadge trace={trace} latestToolError={latestToolError} />
          </div>
        </div>
        <button
          onClick={rerunLatest}
          disabled={!run || inFlight}
          className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          New Chat
        </button>
      </div>

      <div className="border-b border-slate-200 bg-white p-3">
        <div className="grid grid-cols-2 gap-2">
          <PrimaryAgentButton
            label="Analyze this run"
            icon="run"
            disabled={!run || inFlight}
            onClick={() => runAnalysis('run')}
          />
          <PrimaryAgentButton
            label="Analyze this step"
            icon="step"
            active
            disabled={!run || selectedStepIndex === null || inFlight}
            onClick={() => runAnalysis('step')}
          />
        </div>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto bg-slate-50/70 p-3">
        <ObservationSummaryPanel
          run={run}
          trace={trace}
          draft={evalCaseDraft}
          onSelectStep={onSelectStep}
          onOpenTraceEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
        />

        <TraceHistory
          trace={trace}
          pendingUserMessage={pendingUserMessage}
          inFlight={inFlight}
          panelError={panelError}
          expandedEvents={expandedEvents}
          onToggleEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
          onSelectStep={onSelectStep}
        />

        <EvalCaseDraftPanel
          draft={evalCaseDraft}
          onDraftChange={onDraftChange}
          onSelectStep={onSelectStep}
          onValidated={onEvalCaseValidated}
        />

        <div className="flex gap-2">
          <button
            onClick={() => setFeedback('up')}
            disabled={!hasTrace}
            className={`rounded-md border px-2 py-1 text-slate-500 disabled:cursor-not-allowed disabled:opacity-40 ${feedback === 'up' ? 'border-emerald-300 bg-emerald-50 text-emerald-700' : 'border-slate-200 bg-white hover:bg-slate-50'}`}
            title="Helpful"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M7 11v10H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h3Zm0 0 5-8a2 2 0 0 1 3.7 1.3L15 9h4.3a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 18 21H7V11Z" /></svg>
          </button>
          <button
            onClick={() => setFeedback('down')}
            disabled={!hasTrace}
            className={`rounded-md border px-2 py-1 text-slate-500 disabled:cursor-not-allowed disabled:opacity-40 ${feedback === 'down' ? 'border-red-300 bg-red-50 text-red-700' : 'border-slate-200 bg-white hover:bg-slate-50'}`}
            title="Not helpful"
          >
            <svg className="h-4 w-4 rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M7 11v10H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h3Zm0 0 5-8a2 2 0 0 1 3.7 1.3L15 9h4.3a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 18 21H7V11Z" /></svg>
          </button>
        </div>
      </div>

      <div className="border-t border-slate-200 bg-white p-3">
        <div className="mb-2 flex flex-wrap gap-1.5">
          {chipTemplates.map((chip) => (
            <button
              key={chip.label}
              onClick={() => setInput(chip.text)}
              disabled={!hasTrace || chip.disabled || inFlight}
              className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-45"
            >
              {chip.label}
            </button>
          ))}
        </div>
        <div className="relative">
          <textarea
            value={input}
            onChange={(event) => setInput(event.target.value.slice(0, 2000))}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendFollowup();
              }
            }}
            disabled={inputDisabled}
            rows={1}
            maxLength={2000}
            placeholder={hasTrace ? 'Ask about this run...' : 'Run an analysis first to start a conversation.'}
            className="max-h-28 min-h-10 w-full resize-none rounded-lg border border-slate-200 bg-white py-2 pl-3 pr-11 text-sm text-slate-800 shadow-sm outline-none placeholder:text-slate-400 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400"
          />
          <button
            onClick={sendFollowup}
            disabled={inputDisabled || !input.trim()}
            className="absolute bottom-1.5 right-1.5 flex h-7 w-7 items-center justify-center rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            title="Send follow-up"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M5 12h14m-6-6 6 6-6 6" />
            </svg>
          </button>
        </div>
        <div className="mt-2 text-center text-[10px] text-slate-400">AI can make mistakes. Please verify important information.</div>
      </div>
    </aside>
  );
}

function PrimaryAgentButton({
  label,
  icon,
  active = false,
  disabled,
  onClick,
}: {
  label: string;
  icon: 'run' | 'step';
  active?: boolean;
  disabled: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`flex min-h-11 items-center gap-2 rounded-lg border px-3 py-2 text-left text-xs font-semibold shadow-sm transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
        active
          ? 'border-indigo-200 bg-indigo-50 text-indigo-700 hover:bg-indigo-100'
          : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50'
      }`}
    >
      <span className={`flex h-6 w-6 items-center justify-center rounded-md ${active ? 'bg-white text-indigo-700' : 'bg-slate-100 text-slate-500'}`}>
        {icon === 'run' ? (
          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M13 3 4 14h7l-1 7 9-11h-7l1-7Z" /></svg>
        ) : (
          <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="m21 21-4.35-4.35M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Z" /></svg>
        )}
      </span>
      {label}
    </button>
  );
}

function ObservationSummaryPanel({
  run,
  trace,
  draft,
  onSelectStep,
  onOpenTraceEvent,
}: {
  run: TrajectoryRun | null;
  trace: AgentTrace | null;
  draft: EvalCase | null;
  onSelectStep: (index: number) => void;
  onOpenTraceEvent: (seq: number) => void;
}) {
  if (!draft) {
    return (
      <section className="rounded-lg border border-slate-200 bg-white p-3 shadow-sm">
        <h3 className="text-sm font-bold text-slate-900">Observation Summary</h3>
        <p className="mt-2 text-sm leading-5 text-slate-500">No findings yet. Run an analysis to produce a trace and eval-case draft.</p>
      </section>
    );
  }

  const stale = trace?.terminated_by && trace.terminated_by !== 'propose_eval_case';
  const visualEvidence = draft.evidence.filter((item) => isVisualEvidence(item));
  const unavailable = draft.evidence.filter((item) => item.source === 'unavailable');
  const isSuccess = draft.failure_type === null;
  const displayStep = typeof draft.failure_step === 'number' ? draft.failure_step + 1 : null;
  const headerTitle = isSuccess
    ? 'Analysis Result (Success)'
    : `Analysis Result (Step ${displayStep ?? '?'})`;

  return (
    <section className={`rounded-lg border bg-white shadow-sm ${stale ? 'border-amber-200' : 'border-slate-200'}`}>
      <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <h3 className={`text-sm font-bold ${isSuccess ? 'text-emerald-700' : 'text-indigo-700'}`}>{headerTitle}</h3>
        {stale && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">stale</span>}
      </div>
      <div className="space-y-3 p-3 text-sm text-slate-700">
        {draft.actual_behavior && <p className="leading-5">{draft.actual_behavior}</p>}
        {draft.expected_behavior && <p className="text-xs leading-5 text-slate-500">Expected: {draft.expected_behavior}</p>}
        {isSuccess && (
          <p className="leading-5 text-emerald-700">The agent concluded this trajectory completed the task successfully.</p>
        )}

        <div>
          <h4 className="mb-1.5 text-xs font-bold uppercase tracking-wide text-slate-500">Findings</h4>
          <ul className="space-y-1.5">
            {draft.evidence.map((item, index) => (
              <li key={`${item.claim}-${index}`} className="flex gap-2 leading-5">
                <WarningIcon muted={item.source === 'unavailable'} />
                <button
                  onClick={() => {
                    if (typeof item.step_index === 'number') onSelectStep(item.step_index);
                    if (typeof item.trace_event_seq === 'number') onOpenTraceEvent(item.trace_event_seq);
                  }}
                  className="text-left hover:text-indigo-700"
                >
                  {item.claim}
                </button>
              </li>
            ))}
          </ul>
        </div>

        {!isSuccess && draft.failure_type && (
          <div>
            <h4 className="mb-1.5 text-xs font-bold uppercase tracking-wide text-slate-500">Suggested Failure Label</h4>
            <span className="rounded-md border border-red-100 bg-red-50 px-2 py-1 font-mono text-xs font-semibold text-red-700">{draft.failure_type}</span>
          </div>
        )}

        {visualEvidence.length > 0 && (
          <div>
            <h4 className="mb-1.5 text-xs font-bold uppercase tracking-wide text-slate-500">Visual Evidence</h4>
            <div className="flex flex-wrap gap-2">
              {visualEvidence.map((item, index) => {
                const step = typeof item.step_index === 'number'
                  ? run?.steps.find((candidate) => candidate.index === item.step_index)
                  : null;
                const screenshot = step?.observation.screenshot;
                if (!run || !step || !screenshot) return null;
                return (
                  <button
                    key={`${item.claim}-${index}`}
                    onClick={() => onSelectStep(step.index)}
                    className="h-12 w-20 overflow-hidden rounded-md border border-slate-200 bg-slate-100 shadow-sm hover:border-indigo-300"
                    title={item.claim}
                  >
                    <img src={`/api/runs/${run.run_id}/screenshots/${screenshot}`} alt={`Evidence step ${step.index + 1}`} className="h-full w-full object-cover" />
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {draft.retrieved_context_ids.length > 0 && (
          <div>
            <h4 className="mb-1.5 text-xs font-bold uppercase tracking-wide text-slate-500">Retrieved Context</h4>
            <div className="flex flex-wrap gap-1.5">
              {draft.retrieved_context_ids.map((id) => (
                <span key={id} className="rounded-full border border-indigo-100 bg-indigo-50 px-2 py-1 font-mono text-[11px] text-indigo-700">{id}</span>
              ))}
            </div>
          </div>
        )}

        {unavailable.length > 0 && (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
            {unavailable.length} evidence item{unavailable.length === 1 ? '' : 's'} marked unavailable.
          </div>
        )}
      </div>
    </section>
  );
}

function TraceHistory({
  trace,
  pendingUserMessage,
  inFlight,
  panelError,
  expandedEvents,
  onToggleEvent,
  onSelectStep,
}: {
  trace: AgentTrace | null;
  pendingUserMessage: string | null;
  inFlight: boolean;
  panelError: string | null;
  expandedEvents: Set<number>;
  onToggleEvent: (seq: number) => void;
  onSelectStep: (index: number) => void;
}) {
  const rows = traceRows(trace?.events ?? []);

  return (
    <section className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <h3 className="text-sm font-bold text-slate-900">Trace Timeline</h3>
        {trace && <span className="text-[11px] text-slate-500">{trace.tool_call_count} tool calls · {trace.turn_count} turns</span>}
      </div>
      <div className="space-y-3 p-3">
        {!trace && !panelError && <p className="text-sm text-slate-500">Trace events will appear here as the agent calls tools.</p>}
        {rows.map((row) => (
          <TraceRow
            key={row.event.seq}
            row={row}
            expanded={expandedEvents.has(row.event.seq)}
            onToggle={() => onToggleEvent(row.event.seq)}
            onSelectStep={onSelectStep}
          />
        ))}
        {pendingUserMessage && <MessageBubble align="right" message={pendingUserMessage} muted />}
        {inFlight && <TypingIndicator />}
        {panelError && (
          <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700">
            {panelError}
          </div>
        )}
      </div>
    </section>
  );
}

interface TraceRowModel {
  event: AgentTraceEvent;
  result?: AgentTraceEvent;
  error?: AgentTraceEvent;
}

function traceRows(events: AgentTraceEvent[]) {
  const rows: TraceRowModel[] = [];
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    if (event.type === 'tool_call') {
      const next = events[index + 1];
      if (next?.name === event.name && next.type === 'tool_result') {
        rows.push({ event, result: next });
        index += 1;
      } else if (next?.name === event.name && next.type === 'tool_error') {
        rows.push({ event, error: next });
        index += 1;
      } else {
        rows.push({ event });
      }
    } else {
      rows.push({ event });
    }
  }
  return rows;
}

function TraceRow({
  row,
  expanded,
  onToggle,
  onSelectStep,
}: {
  row: TraceRowModel;
  expanded: boolean;
  onToggle: () => void;
  onSelectStep: (index: number) => void;
}) {
  const event = row.event;
  if (event.type === 'user_message') return <MessageBubble align="right" message={event.message ?? ''} />;
  if (event.type === 'agent_message') return <MessageBubble align="left" message={event.message ?? ''} />;
  if (event.type === 'tool_error') return <ToolErrorCard event={event} />;
  if (event.type === 'tool_result') return <ToolResultCard event={event} expanded={expanded} onToggle={onToggle} />;

  const stepIndex = typeof event.args?.step_index === 'number' ? event.args.step_index : null;
  const resultSummary = row.error?.error ?? summarizeResult(row.result?.result);
  const isTerminal = event.name === 'propose_eval_case';

  return (
    <div className={`rounded-lg border p-2 text-xs ${row.error ? 'border-red-200 bg-red-50' : 'border-slate-200 bg-slate-50'}`}>
      <button
        onClick={() => {
          onToggle();
          if (event.name === 'get_step_detail' && stepIndex !== null) onSelectStep(stepIndex);
        }}
        className="flex w-full items-start justify-between gap-3 text-left"
      >
        <div className="min-w-0">
          <div className="font-mono font-semibold text-slate-800">{event.name}</div>
          <div className="mt-1 truncate text-slate-500">{summarizeArgs(event.args)}</div>
          {resultSummary && <div className={`mt-1 ${row.error ? 'text-red-700' : 'text-slate-600'}`}>{resultSummary}</div>}
        </div>
        <span className="shrink-0 text-slate-400">#{event.seq}</span>
      </button>
      {isTerminal && (
        <button
          onClick={() => document.getElementById('eval-case-draft')?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
          className="mt-2 w-full rounded-md border border-slate-200 bg-white py-1.5 text-xs font-semibold text-slate-700 hover:bg-slate-50"
        >
          View Draft
        </button>
      )}
      {expanded && (
        <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-white p-2 text-[11px] leading-4 text-slate-700">
{JSON.stringify({ args: event.args, result: row.result?.result, error: row.error?.error }, null, 2)}
        </pre>
      )}
    </div>
  );
}

function MessageBubble({ align, message, muted = false }: { align: 'left' | 'right'; message: string; muted?: boolean }) {
  return (
    <div className={`flex ${align === 'right' ? 'justify-end' : 'justify-start'}`}>
      <div className={`max-w-[85%] rounded-lg px-3 py-2 text-sm leading-5 ${
        align === 'right'
          ? 'bg-indigo-600 text-white'
          : 'border border-slate-200 bg-white text-slate-700'
      } ${muted ? 'opacity-70' : ''}`}>
        {message || '(empty message)'}
      </div>
    </div>
  );
}

function ToolErrorCard({ event }: { event: AgentTraceEvent }) {
  return (
    <div className="rounded-lg border border-red-200 bg-red-50 p-2 text-xs text-red-700">
      <div className="font-mono font-semibold">{event.name ?? 'tool_error'}</div>
      <div className="mt-1">{event.error}</div>
    </div>
  );
}

function ToolResultCard({
  event,
  expanded,
  onToggle,
}: {
  event: AgentTraceEvent;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50 p-2 text-xs">
      <button onClick={onToggle} className="flex w-full items-center justify-between gap-3 text-left">
        <span className="font-mono font-semibold text-slate-800">{event.name ?? 'tool_result'}</span>
        <span className="text-slate-400">#{event.seq}</span>
      </button>
      <div className="mt-1 text-slate-600">{summarizeResult(event.result)}</div>
      {expanded && (
        <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-white p-2 text-[11px] leading-4 text-slate-700">
{JSON.stringify(event.result, null, 2)}
        </pre>
      )}
    </div>
  );
}

function EvalCaseDraftPanel({
  draft,
  onDraftChange,
  onSelectStep,
  onValidated,
}: {
  draft: EvalCase | null;
  onDraftChange: (draft: EvalCase | null) => void;
  onSelectStep: (index: number) => void;
  onValidated?: () => void;
}) {
  const [localDraft, setLocalDraft] = useState<EvalCase | null>(draft);
  const [dirty, setDirty] = useState(false);
  const [exportStatus, setExportStatus] = useState<string | null>(null);
  const dirtyRef = useRef(false);
  const localDraftRef = useRef<EvalCase | null>(draft);

  useEffect(() => {
    if (!draft) {
      setLocalDraft(null);
      localDraftRef.current = null;
      setDirty(false);
      dirtyRef.current = false;
      setExportStatus(null);
      return;
    }
    if (dirtyRef.current && JSON.stringify(draft) !== JSON.stringify(localDraftRef.current)) {
      const overwrite = window.confirm('The agent produced a new draft. Replace your unsaved edits?');
      if (!overwrite) return;
    }
    setLocalDraft(draft);
    localDraftRef.current = draft;
    setDirty(false);
    dirtyRef.current = false;
    setExportStatus(null);
  }, [draft]);

  if (!localDraft) {
    return null;
  }

  const update = (patch: Partial<EvalCase>) => {
    const next = { ...localDraft, ...patch };
    setLocalDraft(next);
    localDraftRef.current = next;
    setDirty(true);
    dirtyRef.current = true;
    onDraftChange(next);
  };

  const updateEvidenceClaim = (index: number, claim: string) => {
    const evidence = localDraft.evidence.map((item, itemIndex) => itemIndex === index ? { ...item, claim } : item);
    update({ evidence });
  };

  const exportDraft = async () => {
    if (!localDraft.human_validated) return;
    setExportStatus('Exporting...');
    try {
      const saved = await createEvalCase(localDraft);
      setLocalDraft(saved);
      localDraftRef.current = saved;
      onDraftChange(saved);
      setDirty(false);
      dirtyRef.current = false;
      setExportStatus('Exported validated eval case.');
      onValidated?.();
    } catch (error) {
      setExportStatus(errorMessage(error));
    }
  };

  return (
    <section id="eval-case-draft" className="rounded-lg border border-slate-200 bg-white shadow-sm">
      <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <h3 className="text-sm font-bold text-slate-900">Eval Case Draft</h3>
        {dirty && <span className="text-[11px] text-amber-600">edited</span>}
      </div>
      <div className="space-y-3 p-3 text-xs text-slate-700">
        <DraftField label="Case ID" value={localDraft.case_id} readOnly />
        <DraftField label="Source Run" value={localDraft.source_run_id} readOnly />
        {localDraft.failure_type === null ? (
          <div className="rounded-md border border-emerald-100 bg-emerald-50 px-2 py-2 text-[11px] text-emerald-800">
            Success case — the Eval Agent found no failure. Validating this draft marks the run as successful and indexes it for find_similar_successful_run.
          </div>
        ) : (
          <>
            <DraftNumberField label="Failure Step" value={localDraft.failure_step ?? 0} onChange={(value) => update({ failure_step: value })} />
            <DraftField label="Failure Type" value={localDraft.failure_type ?? ''} onChange={(value) => update({ failure_type: value })} />
            <DraftTextArea label="Expected Behavior" value={localDraft.expected_behavior ?? ''} onChange={(value) => update({ expected_behavior: value })} />
            <DraftTextArea label="Actual Behavior" value={localDraft.actual_behavior ?? ''} onChange={(value) => update({ actual_behavior: value })} />
          </>
        )}

        <div>
          <div className="mb-1 text-[11px] font-bold uppercase tracking-wide text-slate-500">Evidence</div>
          <div className="space-y-2">
            {localDraft.evidence.map((item, index) => (
              <div key={`${item.source}-${index}`} className="rounded-md border border-slate-200 bg-slate-50 p-2">
                <input
                  value={item.claim}
                  onChange={(event) => updateEvidenceClaim(index, event.target.value)}
                  className="mb-2 w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
                />
                <div className="flex flex-wrap items-center gap-1.5">
                  <SourceBadge source={item.source} />
                  {typeof item.step_index === 'number' && (
                    <button onClick={() => onSelectStep(item.step_index as number)} className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] text-slate-600 hover:text-indigo-700">
                      step {item.step_index + 1}
                    </button>
                  )}
                  {typeof item.trace_event_seq === 'number' && <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] text-slate-500">trace #{item.trace_event_seq}</span>}
                  {item.context_id && <span className="rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 font-mono text-[10px] text-indigo-700">{item.context_id}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>

        {localDraft.failure_type !== null && (
          <DraftTextArea label="Regression Rule" value={localDraft.regression_rule ?? ''} onChange={(value) => update({ regression_rule: value })} />
        )}
        <DraftField
          label="Retrieved Context IDs"
          value={localDraft.retrieved_context_ids.join(', ')}
          onChange={(value) => update({ retrieved_context_ids: value.split(',').map((item) => item.trim()).filter(Boolean) })}
        />

        <label className="flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 p-2 text-sm font-semibold text-slate-700">
          <input
            type="checkbox"
            checked={localDraft.human_validated}
            onChange={(event) => update({ human_validated: event.target.checked })}
            className="h-4 w-4 rounded border-slate-300"
          />
          Mark validated
        </label>
        <button
          onClick={exportDraft}
          disabled={!localDraft.human_validated}
          className="w-full rounded-md bg-indigo-600 px-3 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          Export Eval Case
        </button>
        {exportStatus && <div className="text-xs text-slate-500">{exportStatus}</div>}
      </div>
    </section>
  );
}

function DraftField({
  label,
  value,
  readOnly = false,
  onChange,
}: {
  label: string;
  value: string;
  readOnly?: boolean;
  onChange?: (value: string) => void;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500">{label}</span>
      <input
        value={value}
        readOnly={readOnly}
        onChange={(event) => onChange?.(event.target.value)}
        className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs outline-none read-only:bg-slate-50 read-only:text-slate-500 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
      />
    </label>
  );
}

function DraftNumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500">{label}</span>
      <input
        type="number"
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
        className="w-full rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
      />
    </label>
  );
}

function DraftTextArea({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[11px] font-bold uppercase tracking-wide text-slate-500">{label}</span>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={3}
        className="w-full resize-y rounded-md border border-slate-200 bg-white px-2 py-1.5 text-xs leading-5 outline-none focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500"
      />
    </label>
  );
}

function TerminationBadge({ trace, latestToolError }: { trace: AgentTrace | null; latestToolError: string | null }) {
  if (!trace) return <div className="mt-0.5 text-[11px] text-slate-400">No trace yet</div>;
  const classes = trace.terminated_by === 'propose_eval_case'
    ? 'bg-emerald-50 text-emerald-700'
    : trace.terminated_by === 'budget_exceeded'
      ? 'bg-slate-100 text-slate-600'
      : 'bg-red-50 text-red-700';
  return (
    <div className="mt-0.5">
      <span title={trace.terminated_by === 'error' ? latestToolError ?? undefined : undefined} className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${classes}`}>
        {trace.terminated_by}
      </span>
    </div>
  );
}

function WarningIcon({ muted = false }: { muted?: boolean }) {
  return (
    <span className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] ${muted ? 'border-amber-300 text-amber-600' : 'border-red-300 text-red-600'}`}>
      !
    </span>
  );
}

function SourceBadge({ source }: { source: EvidenceItem['source'] }) {
  const muted = source === 'unavailable';
  return (
    <span className={`rounded-full px-2 py-0.5 font-mono text-[10px] ${muted ? 'bg-amber-50 text-amber-700' : 'bg-slate-200 text-slate-700'}`}>
      {source}
    </span>
  );
}

function TypingIndicator() {
  return (
    <div className="flex justify-start">
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-500">
        Eval Agent is analyzing...
      </div>
    </div>
  );
}

function promptChips(selectedStepIndex: number | null) {
  return [
    { label: 'Suggest failure label', text: 'Suggest the failure label for this run.' },
    { label: 'Generate eval case', text: 'Generate the eval case draft.' },
    { label: 'Find similar failures', text: 'Find similar failure cases from memory.' },
    { label: 'Compare with another run', text: 'Compare this run with a similar successful run.' },
    {
      label: 'Inspect this step',
      text: selectedStepIndex === null ? '' : `Inspect step ${selectedStepIndex} in detail.`,
      disabled: selectedStepIndex === null,
    },
    { label: 'Explain your reasoning', text: 'Explain why you flagged the failure step.' },
  ];
}

function emptyTrace(runId: string, mode: AnalyzeMode, selectedStepIndex: number | null): AgentTrace {
  return {
    run_id: runId,
    user_intent: mode === 'run' ? 'analyze_run' : 'analyze_step',
    selected_step: mode === 'step' ? selectedStepIndex ?? undefined : undefined,
    tool_call_count: 0,
    turn_count: 1,
    terminated_by: 'error',
    events: [],
  };
}

function appendEvent(runId: string, mode: AnalyzeMode, selectedStepIndex: number | null, event: AgentTraceEvent) {
  return (current: AgentTrace | null): AgentTrace => {
    const base = current ?? emptyTrace(runId, mode, selectedStepIndex);
    if (base.events.some((item) => item.seq === event.seq)) return base;
    return { ...base, events: [...base.events, event] };
  };
}

function isVisualEvidence(item: EvidenceItem) {
  return (
    typeof item.step_index === 'number' &&
    ['trajectory', 'step_detail_high', 'successful_run'].includes(item.source)
  );
}

function summarizeArgs(args?: Record<string, unknown>) {
  if (!args) return 'no args';
  return shorten(JSON.stringify(args), 120);
}

function summarizeResult(result?: Record<string, unknown>) {
  if (!result) return null;
  if (typeof result.tool_error === 'string') return result.tool_error;
  if (typeof result.case_id === 'string') return `draft ${result.case_id}`;
  if (typeof result.failure_type === 'string') return `failure_type: ${result.failure_type}`;
  if (typeof result.vlm_summary === 'string') return shorten(result.vlm_summary, 120);
  if (Array.isArray(result.items)) return `${result.items.length} item${result.items.length === 1 ? '' : 's'}`;
  if (typeof result.has_screenshot === 'boolean') return result.has_screenshot ? 'screenshot available' : 'screenshot unavailable';
  return shorten(JSON.stringify(result), 120);
}

function shorten(value: string, max: number) {
  return value.length > max ? `${value.slice(0, max - 1)}...` : value;
}

function latestError(trace: AgentTrace | null) {
  for (const event of [...(trace?.events ?? [])].reverse()) {
    if (event.type === 'tool_error' && event.error) return event.error;
  }
  return null;
}

function toggleSet(current: Set<number>, value: number) {
  const next = new Set(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

function isAbortError(error: unknown) {
  return error instanceof DOMException && error.name === 'AbortError';
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}
