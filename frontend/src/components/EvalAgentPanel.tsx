import { useEffect, useMemo, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import { createEvalCase, fetchRunDigest } from '../api/client';
import { streamAgentRequest } from '../api/stream';
import type { AgentTrace, AgentTraceEvent, EvalCase, EvidenceItem, TrajectoryDigest, TrajectoryRun } from '../types/contracts';

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
  const [draftViewed, setDraftViewed] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const latestToolError = useMemo(() => latestError(trace), [trace]);
  const hasTrace = Boolean(trace && trace.turn_count > 0);

  // Auto-grow the textarea up to its max-height (112px = max-h-28) so
  // multi-line followups are visible while typing instead of scrolling
  // inside a single-line box.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 112)}px`;
  }, [input]);

  useEffect(() => {
    abortRef.current?.abort();
    setInput('');
    setPanelError(null);
    setPendingUserMessage(null);
    setExpandedEvents(new Set());
    setFeedback(null);
    setDraftViewed(false);
  }, [run?.run_id]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const runAnalysis = async () => {
    if (!run || inFlight) return;

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setInFlight(true);
    setPanelError(null);
    setPendingUserMessage(null);
    setDraftViewed(false);
    onDraftChange(null);
    onTraceChange(emptyTrace(run.run_id));

    try {
      const done = await streamAgentRequest(`/api/runs/${run.run_id}/analyze`, {
        signal: controller.signal,
        onEvent: (event) => onTraceChange(appendEvent(run.run_id, event)),
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
          onTraceChange(appendEvent(run.run_id, event));
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
    runAnalysis();
  };

  // Prefer agent-suggested chips from the latest propose_eval_case event's
  // tool_call.args.suggested_followups (transport-only, not persisted).
  // Falls back to the hard-coded promptChips list when the agent did not
  // supply any (e.g., older traces, success cases the model didn't bother
  // to annotate, model that ignored the optional field).
  const chipTemplates = useMemo(() => {
    const agentChips = extractAgentSuggestions(trace);
    return agentChips.length > 0 ? agentChips : promptChips(selectedStepIndex);
  }, [trace, selectedStepIndex]);

  // Split events by turn so the trace renders in two regions sandwiching
  // the verdict (Summary + draft). Initial analyze events stay above;
  // followup chat events go below the View Draft button so each new
  // followup feels like an append-only conversation.
  const initialTurnEvents = useMemo(
    () => trace?.events.filter((event) => event.turn === 0) ?? [],
    [trace],
  );
  const followupEvents = useMemo(
    () => trace?.events.filter((event) => event.turn > 0) ?? [],
    [trace],
  );
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
            <TerminationBadge trace={trace} latestToolError={latestToolError} inFlight={inFlight} />
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
        <PrimaryAgentButton
          label="Analyze trajectory"
          icon="run"
          active
          disabled={!run || inFlight}
          onClick={runAnalysis}
        />
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto bg-slate-50/70 p-3">
        {/* Progressive disclosure with three regions:
              1. Initial analyze events (turn 0)  — above
              2. Verdict: Observation Summary + View Draft + (optional) Draft editor
              3. Followup chat (turn >= 1)         — below

            Splitting turn-0 above and turn-1+ below makes followup feel
            chat-like: the verdict stays put, new messages append at the
            bottom. Before the split, every followup triggered inFlight,
            hid the Summary/View Draft, and reflowed the layout. */}
        <TraceHistory
          events={initialTurnEvents}
          pendingUserMessage={null}
          inFlight={inFlight && followupEvents.length === 0 && pendingUserMessage === null}
          panelError={!evalCaseDraft ? panelError : null}
          expandedEvents={expandedEvents}
          onToggleEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
          onSelectStep={onSelectStep}
          runId={run?.run_id ?? null}
        />

        {/* Summary + draft persist across followups: no !inFlight gate.
            The presence of evalCaseDraft is the sole condition — once the
            initial analyze ends with propose_eval_case, the verdict is
            "what's on the table" until the user re-analyzes. */}
        {evalCaseDraft && (
          <ObservationSummaryPanel
            run={run}
            trace={trace}
            draft={evalCaseDraft}
            onSelectStep={onSelectStep}
            onOpenTraceEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
          />
        )}

        {evalCaseDraft && !draftViewed && (
          <button
            onClick={() => {
              setDraftViewed(true);
              requestAnimationFrame(() => {
                document.getElementById('eval-case-draft')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              });
            }}
            className="w-full rounded-md border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-semibold text-indigo-700 hover:bg-indigo-100"
          >
            View draft eval case →
          </button>
        )}

        {draftViewed && (
          <EvalCaseDraftPanel
            draft={evalCaseDraft}
            onDraftChange={onDraftChange}
            onSelectStep={onSelectStep}
            onValidated={onEvalCaseValidated}
          />
        )}

        {/* Followup chat region: turn >= 1 events plus pendingUserMessage
            and any in-flight indicator. Empty by default — only appears
            after the user sends a followup. */}
        <TraceHistory
          events={followupEvents}
          pendingUserMessage={pendingUserMessage}
          inFlight={inFlight && (followupEvents.length > 0 || pendingUserMessage !== null)}
          panelError={evalCaseDraft ? panelError : null}
          expandedEvents={expandedEvents}
          onToggleEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
          onSelectStep={onSelectStep}
          runId={run?.run_id ?? null}
        />

        {/* Thumbs feedback only shown once there is a finished trace to react
            to. hasTrace alone goes true the moment emptyTrace is created on
            Analyze click, which is too early — the user is still watching the
            agent work. Gate on !inFlight so thumbs appear only after the
            stream completes (or after a failure, where the user might want
            to thumb-down). */}
        {hasTrace && !inFlight && (
          <div className="flex gap-2">
            <button
              onClick={() => setFeedback('up')}
              className={`rounded-md border px-2 py-1 text-slate-500 ${feedback === 'up' ? 'border-emerald-300 bg-emerald-50 text-emerald-700' : 'border-slate-200 bg-white hover:bg-slate-50'}`}
              title="Helpful"
            >
              <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M7 11v10H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h3Zm0 0 5-8a2 2 0 0 1 3.7 1.3L15 9h4.3a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 18 21H7V11Z" /></svg>
            </button>
            <button
              onClick={() => setFeedback('down')}
              className={`rounded-md border px-2 py-1 text-slate-500 ${feedback === 'down' ? 'border-red-300 bg-red-50 text-red-700' : 'border-slate-200 bg-white hover:bg-slate-50'}`}
              title="Not helpful"
            >
              <svg className="h-4 w-4 rotate-180" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M7 11v10H4a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2h3Zm0 0 5-8a2 2 0 0 1 3.7 1.3L15 9h4.3a2 2 0 0 1 2 2.3l-1.2 8A2 2 0 0 1 18 21H7V11Z" /></svg>
            </button>
          </div>
        )}
      </div>

      <div className="border-t border-slate-200 bg-white p-3">
        {/* Prompt chips are only meaningful as followup shortcuts AFTER a
            trace exists AND the agent is idle. Hiding during inFlight
            matches the chat input (also disabled) and avoids dangling
            "Suggest failure label" buttons while the failure is still
            being computed. */}
        {hasTrace && !inFlight && (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {chipTemplates.map((chip) => (
              <button
                key={chip.label}
                onClick={() => setInput(chip.text)}
                disabled={chip.disabled}
                className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-[11px] font-medium text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-45"
              >
                {chip.label}
              </button>
            ))}
          </div>
        )}
        <div className="flex items-end gap-1.5 rounded-lg border border-slate-200 bg-white px-1.5 py-1.5 shadow-sm focus-within:border-indigo-500 focus-within:ring-1 focus-within:ring-indigo-500">
          <textarea
            ref={textareaRef}
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
            className="block max-h-28 min-h-[1.75rem] w-full flex-1 resize-none overflow-y-auto border-0 bg-transparent px-1.5 py-1 text-sm leading-5 text-slate-800 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed disabled:text-slate-400"
          />
          <button
            onClick={sendFollowup}
            disabled={inputDisabled || !input.trim()}
            className="flex h-7 w-7 shrink-0 items-center justify-center self-end rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
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
  // Dedupe visual evidence by step_index: the agent often emits multiple
  // EvidenceItem entries for the same step (one per claim it derived from
  // the screenshot) and we don't want N copies of the same thumbnail.
  // Keep the first claim as the tooltip; "Findings" still lists every claim.
  const visualEvidence = (() => {
    const seen = new Set<number>();
    const out: EvidenceItem[] = [];
    for (const item of draft.evidence) {
      if (!isVisualEvidence(item)) continue;
      const idx = item.step_index as number;
      if (seen.has(idx)) continue;
      seen.add(idx);
      out.push(item);
    }
    return out;
  })();
  const unavailable = draft.evidence.filter((item) => item.source === 'unavailable');
  const isSuccess = draft.failure_type === null;
  // failure_step on the draft is already a 1-based step index (matches
  // source step keys + screenshot filenames). The "Failure attributed to
  // step N" chip is only rendered for failure cases that name a step;
  // success cases or malformed drafts intentionally omit it.
  const displayStep = typeof draft.failure_step === 'number' ? draft.failure_step : null;

  return (
    <section className={`rounded-lg border bg-white shadow-sm ${stale ? 'border-amber-200' : 'border-slate-200'}`}>
      <div className="flex items-center justify-between border-b border-slate-100 px-3 py-2">
        <h3 className={`text-sm font-bold ${isSuccess ? 'text-emerald-700' : 'text-indigo-700'}`}>Analysis Result</h3>
        {stale && <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">stale</span>}
      </div>
      <div className="space-y-3 p-3 text-sm text-slate-700">
        {!isSuccess && displayStep !== null && typeof draft.failure_step === 'number' && (
          <button
            onClick={() => onSelectStep(draft.failure_step as number)}
            className="flex w-full items-center justify-between gap-2 rounded-md border border-red-100 bg-red-50 px-2.5 py-1.5 text-left text-xs font-semibold text-red-700 hover:bg-red-100"
          >
            <span>Failure attributed to <span className="font-bold">step {displayStep}</span></span>
            <span className="text-red-500">→ Jump to step</span>
          </button>
        )}
        {draft.actual_behavior && <p className="break-words leading-5">{draft.actual_behavior}</p>}
        {draft.expected_behavior && <p className="break-words text-xs leading-5 text-slate-500">Expected: {draft.expected_behavior}</p>}
        {isSuccess && (
          <p className="leading-5 text-emerald-700">The agent concluded this trajectory completed the task successfully.</p>
        )}

        <div>
          <h4 className="mb-1.5 text-xs font-bold uppercase tracking-wide text-slate-500">Findings</h4>
          <ul className="space-y-1.5">
            {draft.evidence.map((item, index) => (
              // Agent's claims can include unbroken URLs (e.g. "github.com/search?q=…")
              // that don't have spaces. Without min-w-0 + break-words the long
              // token pushes the flex row past the right edge of the panel.
              <li key={`${item.claim}-${index}`} className="flex gap-2 leading-5">
                <WarningIcon muted={item.source === 'unavailable'} />
                <button
                  onClick={() => {
                    if (typeof item.step_index === 'number') onSelectStep(item.step_index);
                    if (typeof item.trace_event_seq === 'number') onOpenTraceEvent(item.trace_event_seq);
                  }}
                  className="min-w-0 flex-1 break-words text-left hover:text-indigo-700"
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
                    <img src={`/api/runs/${run.run_id}/screenshots/${screenshot}`} alt={`Evidence step ${step.index}`} className="h-full w-full object-cover" />
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
  events,
  pendingUserMessage,
  inFlight,
  panelError,
  expandedEvents,
  onToggleEvent,
  onSelectStep,
  runId,
}: {
  events: AgentTraceEvent[];
  pendingUserMessage: string | null;
  inFlight: boolean;
  panelError: string | null;
  expandedEvents: Set<number>;
  onToggleEvent: (seq: number) => void;
  onSelectStep: (index: number) => void;
  runId: string | null;
}) {
  const rows = traceRows(events);
  // Render nothing when there is nothing to show. The empty right column
  // is the cue for "nothing happened yet"; inFlight keeps the wrapper
  // mounted during the brief gap before the first event lands so we don't
  // flicker null ↔ rendered.
  const hasContent = rows.length > 0 || Boolean(pendingUserMessage) || inFlight || Boolean(panelError);
  if (!hasContent) return null;

  return (
    <section className="space-y-2">
      {rows.map((row) => (
        <TraceRow
          key={row.event.seq}
          row={row}
          expanded={expandedEvents.has(row.event.seq)}
          onToggle={() => onToggleEvent(row.event.seq)}
          onSelectStep={onSelectStep}
          runId={runId}
        />
      ))}
      {pendingUserMessage && <MessageBubble align="right" message={pendingUserMessage} muted />}
      {panelError && (
        <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700">
          {panelError}
        </div>
      )}
    </section>
  );
}

interface TraceRowModel {
  event: AgentTraceEvent;
  result?: AgentTraceEvent;
  error?: AgentTraceEvent;
  phaseDone?: boolean;
}

function traceRows(events: AgentTraceEvent[]): TraceRowModel[] {
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
  // Mark phase rows as resolved once a later non-phase event arrives. This
  // lets the UI flip the spinner to a checkmark without needing a paired
  // phase_done event from the backend.
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (row.event.type !== 'phase') continue;
    const hasFollowup = rows.slice(index + 1).some((later) => later.event.type !== 'phase');
    row.phaseDone = hasFollowup;
  }
  return rows;
}

function TraceRow({
  row,
  expanded,
  onToggle,
  onSelectStep,
  runId,
}: {
  row: TraceRowModel;
  expanded: boolean;
  onToggle: () => void;
  onSelectStep: (index: number) => void;
  runId: string | null;
}) {
  const event = row.event;
  if (event.type === 'phase') return (
    <PhaseRow
      event={event}
      done={Boolean(row.phaseDone)}
      runId={runId}
      expanded={expanded}
      onToggle={onToggle}
    />
  );
  if (event.type === 'user_message') return <MessageBubble align="right" message={event.message ?? ''} />;
  if (event.type === 'agent_message') return <MessageBubble align="left" message={event.message ?? ''} />;
  if (event.type === 'tool_error') return <ToolErrorBullet event={event} />;
  if (event.type === 'tool_result') return null; // results are folded into the matching tool_call row

  const stepIndex = typeof event.args?.step_index === 'number' ? event.args.step_index : null;
  const errored = Boolean(row.error);
  const description = friendlyToolDescription(event, row.result?.result);
  const statusGlyph = errored ? '⚠' : '✓';
  const statusColor = errored ? 'text-red-600' : 'text-emerald-600';

  return (
    <div className="rounded-md border border-slate-200 bg-white">
      <button
        onClick={() => {
          onToggle();
          if (event.name === 'get_step_detail' && stepIndex !== null) onSelectStep(stepIndex);
        }}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-left"
      >
        <ToolGlyph name={event.name ?? ''} />
        <span className="min-w-0 flex-1 truncate text-xs text-slate-700">{description}</span>
        <span className={`shrink-0 text-xs font-semibold ${statusColor}`}>{statusGlyph}</span>
        <span className="shrink-0 text-[10px] text-slate-400">{expanded ? '⌃' : '⌄'}</span>
      </button>
      {expanded && (
        <ToolDetailView
          name={event.name ?? ''}
          args={event.args}
          result={row.result?.result}
          error={row.error?.error}
        />
      )}
    </div>
  );
}

// Friendly per-tool labels for the expanded row header. The row line
// already shows a sentence-style description (e.g. "Searched failure
// memory for ..."); this label gives the expanded section a short
// human-readable title so the underlying tool name (snake_case
// identifier) doesn't leak into the UI as a banner.
const TOOL_FRIENDLY_NAME: Record<string, string> = {
  get_run: 'Run metadata lookup',
  get_step_detail: 'Step detail inspection',
  search_failure_memory: 'Failure memory search',
  search_eval_cases: 'Prior eval-case search',
  find_similar_successful_run: 'Similar successful runs search',
  propose_eval_case: 'Eval-case proposal',
};

function ToolDetailView({
  name,
  args,
  result,
  error,
}: {
  name: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown>;
  error?: string;
}) {
  const friendly = TOOL_FRIENDLY_NAME[name] ?? 'Tool call';
  return (
    <div className="border-t border-slate-100 px-2.5 py-2">
      <div className="mb-1.5 text-[11px] font-semibold text-slate-700">{friendly}</div>
      {error && (
        <div className="mb-2 rounded-md border border-red-200 bg-red-50 px-2 py-1.5 text-[11px] text-red-700">
          <div className="font-semibold">Tool error</div>
          <div className="break-words">{error}</div>
        </div>
      )}
      <ToolDetailBody name={name} args={args} result={result} />
    </div>
  );
}

function ToolDetailBody({
  name,
  args,
  result,
}: {
  name: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown>;
}) {
  const a = args ?? {};
  const r = result ?? {};
  switch (name) {
    case 'get_run': {
      const steps = Array.isArray(r.steps) ? (r.steps as unknown[]).length : null;
      return (
        <DetailTable>
          <DetailRow label="Task">{typeof r.task === 'string' ? r.task : '—'}</DetailRow>
          {typeof r.source === 'string' && <DetailRow label="Source"><code className="text-[10px]">{r.source}</code></DetailRow>}
          {typeof r.status === 'string' && <DetailRow label="Status">{r.status}</DetailRow>}
          {steps !== null && <DetailRow label="Steps">{steps}</DetailRow>}
        </DetailTable>
      );
    }
    case 'get_step_detail': {
      const stepIndex = typeof a.step_index === 'number' ? a.step_index : null;
      const vlm = typeof r.vlm_summary === 'string' ? r.vlm_summary : null;
      const action = r.action && typeof r.action === 'object' ? (r.action as Record<string, unknown>) : null;
      const coord = r.coordinate_validation && typeof r.coordinate_validation === 'object' ? (r.coordinate_validation as Record<string, unknown>) : null;
      const actionType = action && typeof action.type === 'string' ? action.type : null;
      const coordStatus = coord && typeof coord.status === 'string' ? coord.status : null;
      return (
        <DetailTable>
          {stepIndex !== null && <DetailRow label="Step">{stepIndex}</DetailRow>}
          {actionType && <DetailRow label="Action type">{actionType}</DetailRow>}
          {coordStatus && <DetailRow label="Coordinates">{coordStatus}</DetailRow>}
          {vlm ? (
            <DetailRow label="Visual summary" stacked>
              <span className="block whitespace-pre-wrap break-words text-slate-700">{vlm}</span>
            </DetailRow>
          ) : (
            <DetailRow label="Visual summary"><em className="text-slate-400">unavailable</em></DetailRow>
          )}
        </DetailTable>
      );
    }
    case 'search_failure_memory':
    case 'search_eval_cases': {
      const query = typeof a.query === 'string' ? a.query : null;
      const items = Array.isArray(r.items) ? (r.items as Array<Record<string, unknown>>) : [];
      return (
        <DetailTable>
          <DetailRow label="Query" stacked>
            <span className="block break-words text-slate-700">{query ?? '—'}</span>
          </DetailRow>
          <DetailRow label="Matches">{items.length}</DetailRow>
          {items.length > 0 && (
            <DetailRow label="Cases" stacked>
              <ul className="space-y-1">
                {items.map((item, index) => {
                  const caseId = typeof item.case_id === 'string' ? item.case_id : `match_${index}`;
                  const failureType = typeof item.failure_type === 'string' ? item.failure_type : null;
                  const summary = typeof item.summary === 'string' ? item.summary : null;
                  return (
                    <li key={caseId} className="min-w-0 overflow-hidden rounded border border-slate-200 bg-white px-2 py-1 text-[10px] leading-4">
                      <div className="flex flex-wrap items-baseline gap-1.5">
                        <code className="min-w-0 break-all font-mono text-slate-600" title={caseId}>{shortenCaseId(caseId)}</code>
                        {failureType && <span className="rounded bg-red-50 px-1 font-semibold text-red-700">{failureType}</span>}
                      </div>
                      {summary && <div className="mt-0.5 break-words text-slate-500">{summary}</div>}
                    </li>
                  );
                })}
              </ul>
            </DetailRow>
          )}
        </DetailTable>
      );
    }
    case 'find_similar_successful_run': {
      const task = typeof a.task === 'string' ? a.task : null;
      const items = Array.isArray(r.items) ? (r.items as Array<Record<string, unknown>>) : [];
      return (
        <DetailTable>
          {task && (
            <DetailRow label="Task" stacked>
              <span className="block break-words text-slate-700">{task}</span>
            </DetailRow>
          )}
          <DetailRow label="Matches">{items.length}</DetailRow>
          {items.length > 0 && (
            <DetailRow label="Runs" stacked>
              <ul className="space-y-1">
                {items.map((item, index) => {
                  const runId = typeof item.run_id === 'string' ? item.run_id : `match_${index}`;
                  const itemTask = typeof item.task === 'string' ? item.task : null;
                  return (
                    <li key={runId} className="min-w-0 overflow-hidden rounded border border-slate-200 bg-white px-2 py-1 text-[10px] leading-4">
                      <code className="min-w-0 break-all font-mono text-slate-600" title={runId}>{shortRunId(runId)}</code>
                      {itemTask && <div className="mt-0.5 break-words text-slate-500">{itemTask}</div>}
                    </li>
                  );
                })}
              </ul>
            </DetailRow>
          )}
        </DetailTable>
      );
    }
    case 'propose_eval_case': {
      const caseId = typeof r.case_id === 'string' ? r.case_id : null;
      const failureType = typeof r.failure_type === 'string' ? r.failure_type : null;
      const failureStep = typeof r.failure_step === 'number' ? r.failure_step : null;
      const evidence = Array.isArray(r.evidence) ? (r.evidence as Array<Record<string, unknown>>) : [];
      const retrieved = Array.isArray(r.retrieved_context_ids) ? (r.retrieved_context_ids as unknown[]).filter((id) => typeof id === 'string') as string[] : [];
      return (
        <DetailTable>
          {caseId && (
            <DetailRow label="Case ID">
              <code className="block break-all font-mono text-[10px]" title={caseId}>{shortenCaseId(caseId)}</code>
            </DetailRow>
          )}
          <DetailRow label="Verdict">
            {failureType ? (
              <span className="rounded bg-red-50 px-1 text-red-700">failure · {failureType}</span>
            ) : (
              <span className="rounded bg-emerald-50 px-1 text-emerald-700">success</span>
            )}
          </DetailRow>
          {failureStep !== null && <DetailRow label="Failure step">{failureStep}</DetailRow>}
          <DetailRow label="Evidence items">{evidence.length}</DetailRow>
          {retrieved.length > 0 && (
            <DetailRow label="Retrieved context" stacked>
              <div className="flex flex-wrap gap-1">
                {retrieved.map((id) => (
                  <code
                    key={id}
                    title={id}
                    className="max-w-full break-all rounded bg-indigo-50 px-1 font-mono text-[10px] text-indigo-700"
                  >
                    {shortenCaseId(id)}
                  </code>
                ))}
              </div>
            </DetailRow>
          )}
        </DetailTable>
      );
    }
    default:
      return (
        <div className="text-[11px] text-slate-500">
          No friendly view for this tool yet. Use "Show raw payload" below.
        </div>
      );
  }
}

function DetailTable({ children }: { children: ReactNode }) {
  return <dl className="space-y-1.5 text-[11px] leading-4">{children}</dl>;
}

function DetailRow({
  label,
  children,
  stacked = false,
}: {
  label: string;
  children: ReactNode;
  stacked?: boolean;
}) {
  if (stacked) {
    return (
      <div className="space-y-0.5">
        <dt className="text-[10px] font-semibold uppercase tracking-wide text-slate-500">{label}</dt>
        <dd className="text-slate-700">{children}</dd>
      </div>
    );
  }
  return (
    <div className="flex items-baseline gap-2">
      <dt className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="min-w-0 flex-1 break-words text-slate-700">{children}</dd>
    </div>
  );
}

function ToolGlyph({ name }: { name: string }) {
  // Minimal, monochrome SVG glyphs. Could be replaced with an icon
  // library later; deliberately not pulling lucide-react for one panel.
  const map: Record<string, JSX.Element> = {
    get_run: <path d="M4 6h16M4 12h16M4 18h10" strokeWidth="1.8" strokeLinecap="round" />,
    get_step_detail: <path d="M11 4a7 7 0 1 1 0 14 7 7 0 0 1 0-14Zm9 16-4.35-4.35" strokeWidth="1.8" strokeLinecap="round" />,
    find_similar_successful_run: <path d="M4 12h6m4 0h6m-10-6 4 6-4 6" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" fill="none" />,
    search_failure_memory: <path d="m21 21-4.35-4.35M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Z" strokeWidth="1.8" strokeLinecap="round" />,
    search_eval_cases: <path d="m21 21-4.35-4.35M10.5 18a7.5 7.5 0 1 1 0-15 7.5 7.5 0 0 1 0 15Z M10 7v7M7 10h7" strokeWidth="1.8" strokeLinecap="round" />,
    propose_eval_case: <path d="M5 12l5 5L20 7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />,
  };
  return (
    <svg className="h-4 w-4 shrink-0 text-slate-500" viewBox="0 0 24 24" fill="none" stroke="currentColor">
      {map[name] ?? <circle cx="12" cy="12" r="3" />}
    </svg>
  );
}

function PhaseRow({
  event,
  done,
  runId,
  expanded,
  onToggle,
}: {
  event: AgentTraceEvent;
  done: boolean;
  runId: string | null;
  expanded: boolean;
  onToggle: () => void;
}) {
  const message = event.message ?? `Running ${event.name ?? 'phase'}...`;
  const stepCount = typeof event.args?.step_count === 'number' ? event.args.step_count : null;
  const cached = event.args?.cached === true;
  const [digest, setDigest] = useState<TrajectoryDigest | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canExpand = done && Boolean(runId);

  useEffect(() => {
    if (!expanded || !canExpand || digest || loading || error || !runId) return;
    setLoading(true);
    fetchRunDigest(runId)
      .then((data) => setDigest(data))
      .catch((err) => setError(errorMessage(err)))
      .finally(() => setLoading(false));
  }, [canExpand, digest, error, expanded, loading, runId]);

  return (
    <div className="rounded-md border border-slate-200 bg-slate-50">
      <button
        type="button"
        onClick={onToggle}
        disabled={!canExpand}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-left text-xs text-slate-600 disabled:cursor-default"
      >
        {done ? (
          <svg className="h-3.5 w-3.5 shrink-0 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.4" d="M5 12l5 5L20 7" />
          </svg>
        ) : (
          <svg className="h-3.5 w-3.5 shrink-0 animate-spin text-indigo-500" fill="none" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
            <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
          </svg>
        )}
        <span className="min-w-0 flex-1 truncate">{message}</span>
        {cached && (
          <span className="shrink-0 rounded-full bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">
            cached
          </span>
        )}
        {stepCount !== null && (
          <span className="shrink-0 rounded-full bg-white px-1.5 py-0.5 text-[10px] font-mono text-slate-500">
            {stepCount} steps
          </span>
        )}
        {canExpand && (
          <span className="shrink-0 text-[10px] text-slate-400">{expanded ? '⌃' : '⌄'}</span>
        )}
      </button>
      {expanded && canExpand && (
        <div className="border-t border-slate-200 px-2.5 py-2">
          {loading && <div className="text-[11px] text-slate-500">Loading digest...</div>}
          {error && <div className="text-[11px] text-red-600">{error}</div>}
          {digest && <DigestPreview digest={digest} />}
        </div>
      )}
    </div>
  );
}

function DigestPreview({ digest }: { digest: TrajectoryDigest }) {
  return (
    <div className="space-y-2">
      <div className="text-[10px] text-slate-500">
        <span className="font-mono">{digest.preprocess_model ?? 'unknown'}</span>
        {' · '}
        <span>v{digest.preprocess_version}</span>
      </div>
      <ol className="space-y-1.5">
        {digest.steps.map((step) => (
          <li key={step.index} className="rounded border border-slate-200 bg-white px-2 py-1.5 text-[11px] leading-4 text-slate-700">
            <div className="flex flex-wrap items-baseline gap-1.5">
              <span className="font-mono text-slate-500">step {step.index}</span>
              <span className="rounded bg-slate-100 px-1 font-semibold text-slate-700">{step.action_type}</span>
              {step.action_text && <span className="min-w-0 flex-1 break-words text-slate-700">{step.action_text}</span>}
            </div>
            {step.vlm_low_detail_summary && (
              <div className="mt-1 break-words text-slate-500">{step.vlm_low_detail_summary}</div>
            )}
            {!step.vlm_low_detail_summary && !step.has_screenshot && (
              <div className="mt-1 text-[10px] italic text-slate-400">no screenshot — VLM skipped</div>
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function ToolErrorBullet({ event }: { event: AgentTraceEvent }) {
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-2.5 py-2 text-xs text-red-700">
      <div className="flex items-start gap-2">
        <span className="font-semibold">⚠</span>
        <div className="min-w-0 flex-1">
          <div className="font-semibold">{event.name ?? 'Agent error'}</div>
          {event.error && <div className="mt-0.5 break-words text-red-700/90">{event.error}</div>}
        </div>
      </div>
    </div>
  );
}

function friendlyToolDescription(event: AgentTraceEvent, result: Record<string, unknown> | undefined): string {
  const args = event.args ?? {};
  const stepIndex = typeof args.step_index === 'number' ? args.step_index : null;
  const query = typeof args.query === 'string' ? args.query : null;
  const task = typeof args.task === 'string' ? args.task : null;
  const runId = typeof args.run_id === 'string' ? args.run_id : null;
  const itemCount = Array.isArray(result?.items) ? (result!.items as unknown[]).length : null;

  switch (event.name) {
    case 'get_run':
      return runId ? `Loaded run metadata for ${shortRunId(runId)}` : 'Loaded run metadata';
    case 'get_step_detail':
      return stepIndex !== null
        ? `Inspected step ${stepIndex}`
        : 'Inspected a step';
    case 'find_similar_successful_run':
      if (itemCount === 0) return 'Looked for similar successful runs — none yet';
      if (itemCount !== null) return `Found ${itemCount} similar successful run${itemCount === 1 ? '' : 's'}`;
      return task ? `Searching for similar successful runs to: ${shorten(task, 60)}` : 'Searching for similar successful runs';
    case 'search_failure_memory':
      if (itemCount !== null && query) return `Searched failure memory for "${shorten(query, 60)}" — ${itemCount} match${itemCount === 1 ? '' : 'es'}`;
      if (query) return `Searching failure memory for "${shorten(query, 60)}"`;
      return 'Searching failure memory';
    case 'search_eval_cases':
      if (itemCount !== null && query) return `Searched prior eval cases for "${shorten(query, 60)}" — ${itemCount} match${itemCount === 1 ? '' : 'es'}`;
      if (query) return `Searching prior eval cases for "${shorten(query, 60)}"`;
      return 'Searching prior eval cases';
    case 'propose_eval_case': {
      const caseId = typeof result?.case_id === 'string' ? result.case_id : null;
      const isSuccess = result?.failure_type === null || result?.failure_type === undefined;
      if (caseId && isSuccess) return `Drafted success eval case (${shortenCaseId(caseId)})`;
      if (caseId) return `Drafted eval case (${shortenCaseId(caseId)})`;
      return 'Drafting eval case';
    }
    default:
      return event.name ?? '(unknown tool)';
  }
}

function shortRunId(runId: string) {
  return runId.length > 12 ? `${runId.slice(0, 8)}…` : runId;
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
  // Stash of the most recent failure-mode field values so toggling
  // failure → success → failure restores the user's prior edits instead
  // of resetting to placeholders. Cleared whenever a new draft arrives
  // from props (agent re-proposed) so the snapshot can't leak across
  // distinct drafts.
  const lastFailureFieldsRef = useRef<{
    failure_step: number;
    failure_type: string;
    expected_behavior: string;
    actual_behavior: string;
    regression_rule: string;
  } | null>(null);

  useEffect(() => {
    if (!draft) {
      setLocalDraft(null);
      localDraftRef.current = null;
      setDirty(false);
      dirtyRef.current = false;
      setExportStatus(null);
      lastFailureFieldsRef.current = null;
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
    lastFailureFieldsRef.current = null;
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

  const saveDraft = async () => {
    if (!localDraft.human_validated) return;
    setExportStatus('Saving…');
    try {
      const saved = await createEvalCase(localDraft);
      setLocalDraft(saved);
      localDraftRef.current = saved;
      onDraftChange(saved);
      setDirty(false);
      dirtyRef.current = false;
      setExportStatus('Saved. Indexed into RAG; run status updated.');
      onValidated?.();
    } catch (error) {
      setExportStatus(errorMessage(error));
    }
  };

  const isSuccess = localDraft.failure_type === null;
  // Toggling between modes mutates all 5 XOR fields at once (the backend
  // validator rejects half-populated drafts). When switching failure →
  // success we stash the current failure values; switching back restores
  // them so the user doesn't lose unsaved edits to a misclick.
  const setMode = (toSuccess: boolean) => {
    if (toSuccess === isSuccess) return;
    if (toSuccess) {
      lastFailureFieldsRef.current = {
        failure_step: typeof localDraft.failure_step === 'number' ? localDraft.failure_step : 1,
        // failure_type backend pattern is /^[a-z][a-z0-9_]*$/ — fall back
        // to a regex-valid placeholder if the field is somehow empty.
        failure_type: localDraft.failure_type ?? 'unspecified',
        expected_behavior: localDraft.expected_behavior ?? '',
        actual_behavior: localDraft.actual_behavior ?? '',
        regression_rule: localDraft.regression_rule ?? '',
      };
      update({
        failure_step: null,
        failure_type: null,
        expected_behavior: null,
        actual_behavior: null,
        regression_rule: null,
      });
    } else {
      const stash = lastFailureFieldsRef.current;
      update({
        failure_step: stash?.failure_step ?? 1,
        failure_type: stash?.failure_type ?? 'unspecified',
        expected_behavior: stash?.expected_behavior ?? '',
        actual_behavior: stash?.actual_behavior ?? '',
        regression_rule: stash?.regression_rule ?? '',
      });
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

        <div className="flex gap-1 rounded-md border border-slate-200 bg-slate-50 p-1">
          <ModeButton active={!isSuccess} onClick={() => setMode(false)} tone="failure">Failure case</ModeButton>
          <ModeButton active={isSuccess} onClick={() => setMode(true)} tone="success">Success case</ModeButton>
        </div>

        {isSuccess ? (
          <div className="rounded-md border border-emerald-100 bg-emerald-50 px-2 py-2 text-[11px] text-emerald-800">
            Success case — validating this draft marks the run as successful and indexes it for find_similar_successful_run.
          </div>
        ) : (
          <>
            <DraftNumberField
              label="Failure Step"
              value={typeof localDraft.failure_step === 'number' ? localDraft.failure_step : 1}
              onChange={(value) => update({ failure_step: value })}
            />
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
                      step {item.step_index}
                    </button>
                  )}
                  {typeof item.trace_event_seq === 'number' && <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 font-mono text-[10px] text-slate-500">trace #{item.trace_event_seq}</span>}
                  {item.context_id && <span className="rounded-full border border-indigo-100 bg-indigo-50 px-2 py-0.5 font-mono text-[10px] text-indigo-700">{item.context_id}</span>}
                </div>
              </div>
            ))}
          </div>
        </div>

        {!isSuccess && (
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
          onClick={saveDraft}
          disabled={!localDraft.human_validated}
          className="w-full rounded-md bg-indigo-600 px-3 py-2 text-sm font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          title="Persists the validated EvalCase to SQLite (eval_cases table), flips the source run's status, and indexes it into ChromaDB for RAG."
        >
          Save validated case
        </button>
        {exportStatus && <div className="text-xs text-slate-500">{exportStatus}</div>}
      </div>
    </section>
  );
}

function ModeButton({
  active,
  onClick,
  tone,
  children,
}: {
  active: boolean;
  onClick: () => void;
  tone: 'success' | 'failure';
  children: ReactNode;
}) {
  const activeClasses = tone === 'success'
    ? 'bg-emerald-600 text-white shadow-sm'
    : 'bg-red-600 text-white shadow-sm';
  const idleClasses = 'text-slate-600 hover:bg-white hover:text-slate-900';
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 rounded px-2 py-1 text-[11px] font-semibold transition-colors ${active ? activeClasses : idleClasses}`}
    >
      {children}
    </button>
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

function TerminationBadge({ trace, latestToolError, inFlight }: { trace: AgentTrace | null; latestToolError: string | null; inFlight: boolean }) {
  if (!trace) return <div className="mt-0.5 text-[11px] text-slate-400">No trace yet</div>;
  // While the analyze stream is in flight, ignore the placeholder
  // terminated_by="error" baked into emptyTrace — the real terminator
  // only lands when the `done` event arrives. Showing "running" avoids
  // misleading the user that the run failed.
  if (inFlight) {
    return (
      <div className="mt-0.5">
        <span className="rounded-full bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-700">
          running…
        </span>
      </div>
    );
  }
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

type ChipTemplate = { label: string; text: string; disabled?: boolean };

function extractAgentSuggestions(trace: AgentTrace | null): ChipTemplate[] {
  // Walk events backwards to find the latest propose_eval_case tool_call.
  // suggested_followups (if any) is on args of that call. Defensive parsing:
  // unknown / malformed entries are dropped silently — falling back to the
  // hard-coded list is preferable to crashing the chip rail.
  for (const event of [...(trace?.events ?? [])].reverse()) {
    if (event.type === 'tool_call' && event.name === 'propose_eval_case') {
      const raw = event.args?.suggested_followups;
      if (!Array.isArray(raw)) return [];
      const chips: ChipTemplate[] = [];
      for (const item of raw) {
        if (
          item &&
          typeof item === 'object' &&
          typeof (item as { label?: unknown }).label === 'string' &&
          typeof (item as { message?: unknown }).message === 'string'
        ) {
          const { label, message } = item as { label: string; message: string };
          if (label.trim() && message.trim()) {
            chips.push({ label: label.slice(0, 40), text: message.slice(0, 200) });
          }
        }
      }
      return chips.slice(0, 4);
    }
  }
  return [];
}

function promptChips(selectedStepIndex: number | null): ChipTemplate[] {
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
    // Override paths when the user disagrees with the agent's verdict. The
    // followup system prompt explicitly allows re-calling propose_eval_case
    // when revising the draft, so these chips trigger a fresh proposal.
    {
      label: 'Reclassify as success',
      text: 'This run actually succeeded. Please re-propose the eval case as a success case (clear all failure fields).',
    },
    {
      label: 'Reclassify as failure',
      text: 'This run actually failed. Please re-propose the eval case with the correct failure step, failure type, expected behavior, and actual behavior.',
    },
  ];
}

function emptyTrace(runId: string): AgentTrace {
  return {
    run_id: runId,
    user_intent: 'analyze_run',
    selected_step: undefined,
    tool_call_count: 0,
    turn_count: 1,
    terminated_by: 'error',
    events: [],
  };
}

function appendEvent(runId: string, event: AgentTraceEvent) {
  return (current: AgentTrace | null): AgentTrace => {
    const base = current ?? emptyTrace(runId);
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

function shorten(value: string, max: number) {
  return value.length > max ? `${value.slice(0, max - 1)}...` : value;
}

function shortenCaseId(caseId: string) {
  // Case IDs are ec_{64-char-hash}_step_{n} or ec_{64-char-hash}_success.
  // The full hash is unhelpful in a one-line summary. Keep the prefix +
  // first 10 chars of the hash + the suffix (_step_n / _success) so the
  // semantic shape (failure vs success, which step) survives.
  const match = caseId.match(/^(ec_)([A-Za-z0-9_.-]+?)(_step_\d+(?:_[a-z][a-z0-9_]*)?|_success)$/);
  if (!match) return caseId;
  const [, prefix, hash, tail] = match;
  if (hash.length <= 12) return caseId;
  return `${prefix}${hash.slice(0, 10)}…${tail}`;
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
