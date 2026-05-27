import { Fragment, useEffect, useMemo, useRef, useState, type Dispatch, type ReactNode, type SetStateAction } from 'react';
import remarkGfm from 'remark-gfm';
import { Streamdown } from 'streamdown';
import { createEvalCase, fetchRunDigest } from '../api/client';
import { streamAgentRequest, type AgentDelta } from '../api/stream';

// Local helper type for the streamingText Map. Holds the running
// concatenation of token deltas plus the originating turn so the
// bubble can be rendered next to the right message group.
interface StreamingMessage {
  turn: number;
  text: string;
}
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
  // Replaces the old `draftViewed` toggle. Owned by the panel (not the
  // VerdictBlock that hosts the trigger) so it survives layout shifts
  // when the verdict block moves between turns.
  const [verdictModalOpen, setVerdictModalOpen] = useState(false);
  // Once analyze finishes and a draft lands, fold the tool-call timeline
  // into a one-line summary so the Analysis Result moves up to the top
  // of the scroll region. Streaming keeps the timeline expanded so the
  // user can watch the agent work; the auto-collapse triggers on the
  // inFlight true → false transition, and on a page reload that brings
  // up a pre-existing draft.
  const [traceCollapsed, setTraceCollapsed] = useState(false);
  // Per-followup-turn user override for "show tool calls again". Default
  // empty Set = every completed tool run is collapsed. User clicks the
  // toggle row to add/remove that run's first-seq key.
  const [expandedFollowupRuns, setExpandedFollowupRuns] = useState<Set<number>>(new Set());
  // In-flight streaming text per LLM generation. Keyed by stream_id
  // (LangChain AIMessageChunk.id, same across all chunks of one call).
  // An entry stays in the map until either:
  //   (a) the final agent_message trace event lands carrying the same
  //       full content (then we drop the entry to avoid duplicate
  //       rendering — the trace event becomes authoritative), or
  //   (b) inFlight goes false (stream ended; clear all).
  const [streamingText, setStreamingText] = useState<Map<string, StreamingMessage>>(new Map());
  const wasInFlight = useRef(false);
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
    setVerdictModalOpen(false);
    // Run switch: collapse iff a draft is already on the table from the
    // server (page reload landed on a previously-analyzed run).
    setTraceCollapsed(Boolean(evalCaseDraft));
    setExpandedFollowupRuns(new Set());
    setStreamingText(new Map());
    wasInFlight.current = false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.run_id]);

  useEffect(() => {
    // Auto-collapse the moment streaming ends with a draft on the table.
    if (wasInFlight.current && !inFlight && evalCaseDraft) {
      setTraceCollapsed(true);
    }
    wasInFlight.current = inFlight;
  }, [inFlight, evalCaseDraft]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const runAnalysis = async () => {
    if (!run || inFlight) return;

    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setInFlight(true);
    setPanelError(null);
    setPendingUserMessage(null);
    setVerdictModalOpen(false);
    setStreamingText(new Map());
    onDraftChange(null);
    onTraceChange(emptyTrace(run.run_id));

    try {
      const done = await streamAgentRequest(`/api/runs/${run.run_id}/analyze`, {
        signal: controller.signal,
        onEvent: (event) => {
          // The full agent_message event supersedes any streaming
          // buffer with the same content — drop matching entries so
          // the bubble doesn't render twice during the small window
          // between final delta and final event arrival.
          if (event.type === 'agent_message') {
            setStreamingText((current) => dropFinalizedStream(current, event.message ?? ''));
          }
          onTraceChange(appendEvent(run.run_id, event));
        },
        onDelta: (delta) => setStreamingText((current) => appendDelta(current, delta)),
      });
      onTraceChange(done.agent_trace);
      onDraftChange(done.eval_case_draft);
    } catch (error) {
      if (!isAbortError(error)) setPanelError(errorMessage(error));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setInFlight(false);
      setStreamingText(new Map());
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
          if (event.type === 'agent_message') {
            setStreamingText((current) => dropFinalizedStream(current, event.message ?? ''));
          }
          onTraceChange(appendEvent(run.run_id, event));
        },
        onDelta: (delta) => setStreamingText((current) => appendDelta(current, delta)),
      });
      onTraceChange(done.agent_trace);
      if (done.eval_case_draft) onDraftChange(done.eval_case_draft);
    } catch (error) {
      if (!isAbortError(error)) setPanelError(errorMessage(error));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setPendingUserMessage(null);
      setInFlight(false);
      setStreamingText(new Map());
    }
  };

  const rerunLatest = () => {
    if (!run || inFlight) return;
    const shouldRerun = window.confirm('Start a fresh analysis for this trajectory? The current trace view will be replaced.');
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
  // Initial analyze = stream is in flight AND no draft has been produced
  // yet. Used to override the auto-collapse: while the initial analyze
  // is happening, the user needs to see events stream live. Followup
  // streams run with a draft already on the table and don't override.
  const initialAnalyzeStreaming = inFlight && !evalCaseDraft;

  // Locate the latest propose_eval_case tool_call so the verdict trio
  // (ObservationSummaryPanel + View draft button + EvalCaseDraftPanel)
  // can follow it. When the agent reproposes via followup, the verdict
  // moves below the followup chat so the new card appears right after
  // the user's message — instead of silently updating in place above.
  const latestProposeTurn = useMemo(() => {
    for (const event of [...(trace?.events ?? [])].reverse()) {
      if (event.type === 'tool_call' && event.name === 'propose_eval_case') {
        return event.turn;
      }
    }
    return null;
  }, [trace]);

  // Build the verdict trio once and let the parent / FollowupTimeline
  // decide where to slot it. There's exactly one draft at a time, so
  // exactly one rendered position is needed. EvalCaseDraftPanel keeps
  // its own state internally — re-anchoring across followup turns will
  // unmount/remount it, but the user's unsaved edits to the *previous*
  // draft are already stale by the time a new propose_eval_case fires.
  const verdictNode: ReactNode = evalCaseDraft ? (
    <VerdictBlock
      run={run}
      trace={trace}
      draft={evalCaseDraft}
      onSelectStep={onSelectStep}
      onOpenTraceEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
      onOpenDraft={() => setVerdictModalOpen(true)}
    />
  ) : null;

  return (
    <aside className="relative flex max-h-[680px] w-full shrink-0 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm xl:h-full xl:max-h-none xl:w-[410px]">
      <div className="flex items-center gap-2 border-b border-slate-200 bg-white px-3 py-2">
        <h2 className="shrink-0 font-bold text-slate-950">Eval Agent</h2>
        <div className="min-w-0 flex-1">
          <TerminationBadge trace={trace} latestToolError={latestToolError} inFlight={inFlight} />
        </div>
        <AnalyzeButton
          hasTrace={hasTrace}
          inFlight={inFlight}
          disabled={!run}
          trace={trace}
          onAnalyze={runAnalysis}
          onReanalyze={rerunLatest}
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
        {/* Auto-collapse the tool-call timeline once an initial analyze
            has finished with a draft, so the Analysis Result floats to
            the top. The override condition `initialAnalyzeStreaming`
            keeps the timeline visible while the initial analyze is in
            flight (no draft yet); followup streams (which run with the
            draft already on the table) do NOT re-expand it. */}
        {evalCaseDraft && (
          <TraceCollapseToggle
            collapsed={traceCollapsed}
            events={initialTurnEvents}
            // Initial analyze runtime only — turn 0's slice. Falls back
            // to cumulative for legacy traces persisted before
            // turn_metrics existed. Followup runtimes never bump this.
            runtimeMs={
              trace?.turn_metrics?.find((entry) => entry.turn === 0)?.runtime_ms
                ?? trace?.runtime_ms
                ?? 0
            }
            onToggle={() => setTraceCollapsed((value) => !value)}
          />
        )}
        {(initialAnalyzeStreaming || !traceCollapsed || !evalCaseDraft) && (
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
        )}

        {/* Streaming bubbles for the initial analyze (turn 0). Free-text
            agent replies during analyze are rare but supported — render
            them between the tool-call timeline and the verdict so the
            user sees the agent's intermediate text typing in. Followup
            streams (turn >= 1) live inside FollowupTimeline instead. */}
        {Array.from(streamingText.entries())
          .filter(([, value]) => value.turn === 0 && value.text)
          .map(([id, value]) => (
            <MessageBubble key={`stream-${id}`} align="left" message={value.text} />
          ))}

        {/* Verdict trio (Observation summary + View draft button + draft
            editor). There's only one evalCaseDraft at a time, so the trio
            renders in exactly one position — the END of the turn that
            produced the latest propose_eval_case. turn 0 (initial
            analyze) keeps it above the followup chat; followup repropose
            (turn N >= 1) inserts it inside FollowupTimeline right after
            turn N's last event, so an unrelated turn N+1 message can't
            push the verdict to the bottom of the conversation. */}
        {evalCaseDraft && latestProposeTurn === 0 && verdictNode}

        {/* Followup chat region: turn >= 1 events plus pendingUserMessage
            and any in-flight indicator. Each completed followup turn's
            tool calls collapse into a one-line summary; the verdictNode
            is injected after the turn that produced it. */}
        <FollowupTimeline
          events={followupEvents}
          pendingUserMessage={pendingUserMessage}
          inFlight={inFlight && (followupEvents.length > 0 || pendingUserMessage !== null)}
          panelError={evalCaseDraft ? panelError : null}
          expandedEvents={expandedEvents}
          onToggleEvent={(seq) => setExpandedEvents((current) => toggleSet(current, seq))}
          onSelectStep={onSelectStep}
          runId={run?.run_id ?? null}
          expandedRuns={expandedFollowupRuns}
          onToggleRun={(seq) => setExpandedFollowupRuns((current) => toggleSet(current, seq))}
          verdictBlock={
            evalCaseDraft && latestProposeTurn !== null && latestProposeTurn > 0 ? verdictNode : null
          }
          verdictAfterTurn={latestProposeTurn !== null && latestProposeTurn > 0 ? latestProposeTurn : null}
          streamingMessages={streamingText}
        />

        {(hasTrace || inFlight) && <TraceFooter trace={trace} inFlight={inFlight} />}
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
            placeholder={hasTrace ? 'Ask about this trajectory...' : 'Run an analysis first to start a conversation.'}
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
      {/* Modal rendered as the last child of the panel so it overlays
          everything but stays scoped to the right column. Always
          mounted (visibility-toggled via the `open` prop) so the
          inner draft form's local state survives close/reopen. */}
      <VerdictModal
        open={verdictModalOpen && Boolean(evalCaseDraft)}
        onClose={() => setVerdictModalOpen(false)}
      >
        <EvalCaseDraftPanel
          draft={evalCaseDraft}
          onDraftChange={onDraftChange}
          onSelectStep={onSelectStep}
          onValidated={onEvalCaseValidated}
          onClose={() => setVerdictModalOpen(false)}
        />
      </VerdictModal>
    </aside>
  );
}

function VerdictModal({
  open,
  onClose,
  children,
}: {
  open: boolean;
  onClose: () => void;
  children: ReactNode;
}) {
  // Esc key closes the modal. The handler is only attached while
  // open, so we don't intercept Esc when the user is typing
  // elsewhere with the modal closed.
  useEffect(() => {
    if (!open) return undefined;
    const handleKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose();
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [open, onClose]);

  // Always render the children. When closed we just hide the
  // wrapper via display:none, so the draft form's internal state
  // (localDraft, dirty, lastFailureFieldsRef) survives across open
  // and close — closing the modal is non-destructive.
  return (
    <div
      className={open ? 'absolute inset-0 z-40 flex items-center justify-center' : 'hidden'}
      role={open ? 'dialog' : undefined}
      aria-modal={open ? 'true' : undefined}
    >
      <div
        className="absolute inset-0 bg-slate-900/30"
        onClick={onClose}
      />
      <div className="relative z-10 flex max-h-[92%] w-[380px] flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-2xl">
        {children}
      </div>
    </div>
  );
}

function AnalyzeButton({
  hasTrace,
  inFlight,
  disabled,
  trace,
  onAnalyze,
  onReanalyze,
}: {
  hasTrace: boolean;
  inFlight: boolean;
  disabled: boolean;
  trace: AgentTrace | null;
  onAnalyze: () => void;
  onReanalyze: () => void;
}) {
  // While streaming, the button shows "Analyzing…" + a live tool-call
  // counter — runtime ticks live in the TraceFooter at the bottom of
  // the panel, no need to repeat it in the header where it'd compete
  // with the verdict for the user's attention.
  if (inFlight) {
    const toolCalls = trace?.events.filter((event) => event.type === 'tool_call').length ?? 0;
    return (
      <button
        type="button"
        disabled
        className="flex shrink-0 items-center gap-1.5 rounded-md border border-indigo-200 bg-indigo-50 px-3 py-1.5 text-xs font-semibold text-indigo-700 shadow-sm disabled:cursor-wait"
      >
        <svg className="h-3.5 w-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeOpacity="0.25" strokeWidth="3" />
          <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
        </svg>
        <span>Analyzing…</span>
        {toolCalls > 0 && (
          <span className="text-indigo-500/80">· {toolCalls} tool call{toolCalls === 1 ? '' : 's'}</span>
        )}
      </button>
    );
  }

  if (hasTrace) {
    return (
      <button
        type="button"
        onClick={onReanalyze}
        disabled={disabled}
        title="Replace the current analysis with a fresh run"
        className="flex shrink-0 items-center gap-1.5 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-xs font-semibold text-slate-700 shadow-sm hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M3 12a9 9 0 0 1 15.5-6.3M21 4v5h-5M21 12a9 9 0 0 1-15.5 6.3M3 20v-5h5" />
        </svg>
        Re-analyze
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onAnalyze}
      disabled={disabled}
      className="flex shrink-0 items-center gap-1.5 rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
    >
      <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.8" d="M13 3 4 14h7l-1 7 9-11h-7l1-7Z" />
      </svg>
      Analyze
    </button>
  );
}

function VerdictBlock({
  run,
  trace,
  draft,
  onSelectStep,
  onOpenTraceEvent,
  onOpenDraft,
}: {
  run: TrajectoryRun | null;
  trace: AgentTrace | null;
  draft: EvalCase;
  onSelectStep: (index: number) => void;
  onOpenTraceEvent: (seq: number) => void;
  onOpenDraft: () => void;
}) {
  // Inline portion of the verdict: the Analysis Result summary plus a
  // trigger button. The editable draft form (EvalCaseDraftPanel) now
  // lives in the modal rendered at the EvalAgentPanel level — that way
  // the panel's own state survives when this block re-anchors between
  // turns and when the modal is closed/reopened.
  return (
    <>
      <ObservationSummaryPanel
        run={run}
        trace={trace}
        draft={draft}
        onSelectStep={onSelectStep}
        onOpenTraceEvent={onOpenTraceEvent}
      />
      <button
        onClick={onOpenDraft}
        className="w-full rounded-md border border-indigo-200 bg-indigo-50 px-3 py-2 text-sm font-semibold text-indigo-700 hover:bg-indigo-100"
      >
        View / edit draft verdict →
      </button>
    </>
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
      <section>
        <h3 className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Analysis Result</h3>
        <p className="mt-2 text-sm leading-5 text-slate-500">No findings yet. Run an analysis to produce a trace and draft verdict.</p>
      </section>
    );
  }

  const stale = trace?.terminated_by && trace.terminated_by !== 'propose_eval_case';
  // Dedupe visual evidence by step_index: the agent often emits multiple
  // EvidenceItem entries for the same step (one per claim it derived from
  // the screenshot) and we don't want N copies of the same thumbnail.
  // Keep the first claim as the tooltip; the evidence list still lists every claim.
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
  // source step keys + screenshot filenames). The "step N ↗" pill is
  // only rendered for failure cases that name a step; success cases or
  // malformed drafts intentionally omit it.
  const displayStep = typeof draft.failure_step === 'number' ? draft.failure_step : null;

  // No card border — section lives directly on the scroll bg. Whitespace
  // + section labels carry the structure. Reduces "card-on-card" nesting
  // when this section sits below the collapsed trace toggle.
  return (
    <section className="space-y-3">
      {/* Header row: small eyebrow label + optional step-N pill on the right */}
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-[11px] font-bold uppercase tracking-wider text-slate-500">Analysis Result</h3>
        <div className="flex items-center gap-1.5">
          {stale && (
            <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">stale</span>
          )}
          {!isSuccess && displayStep !== null && typeof draft.failure_step === 'number' && (
            <button
              onClick={() => onSelectStep(draft.failure_step as number)}
              className="flex items-center gap-1 rounded-full bg-red-50 px-2 py-0.5 text-[11px] font-semibold text-red-700 hover:bg-red-100"
              title="Jump to the step the agent attributed failure to"
            >
              step {displayStep}
              <span aria-hidden="true">↗</span>
            </button>
          )}
        </div>
      </div>

      {/* Primary headline: actual_behavior promoted to the visual anchor */}
      {draft.actual_behavior && (
        <p className="break-words text-[15px] font-medium leading-6 text-slate-900">
          {draft.actual_behavior}
        </p>
      )}
      {isSuccess && (
        <p className="break-words text-[15px] font-medium leading-6 text-emerald-700">
          The agent concluded this trajectory completed the task successfully.
        </p>
      )}

      {/* Expected behavior in a quiet tinted block with an eyebrow label.
          Visually distinct from the headline without competing with it. */}
      {draft.expected_behavior && (
        <div className="rounded-md bg-slate-100/70 px-3 py-2">
          <div className="mb-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Expected behavior
          </div>
          <p className="break-words text-xs leading-5 text-slate-600">{draft.expected_behavior}</p>
        </div>
      )}

      {/* Supporting evidence: neutral grey bullets via :before pseudo. No
          red ! icons — the page is already about a failure verdict, every
          item being "alarming" added noise without information. */}
      {draft.evidence.length > 0 && (
        <div>
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Supporting evidence
          </div>
          <ul className="ml-1 space-y-2 text-[13px] leading-5 text-slate-700">
            {draft.evidence.map((item, index) => {
              const muted = item.source === 'unavailable';
              return (
                <li
                  key={`${item.claim}-${index}`}
                  className={`relative pl-4 before:absolute before:left-0 before:top-2 before:h-1 before:w-1 before:rounded-full ${muted ? 'before:bg-amber-400' : 'before:bg-slate-400'}`}
                >
                  <button
                    onClick={() => {
                      if (typeof item.step_index === 'number') onSelectStep(item.step_index);
                      if (typeof item.trace_event_seq === 'number') onOpenTraceEvent(item.trace_event_seq);
                    }}
                    className="block w-full min-w-0 break-words text-left hover:text-indigo-700"
                  >
                    {item.claim}
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {!isSuccess && draft.failure_type && (
        <div>
          <div className="mb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Suggested failure label
          </div>
          <span className="rounded bg-red-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-red-700">
            {draft.failure_type}
          </span>
        </div>
      )}

      {visualEvidence.length > 0 && (
        <div>
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Visual evidence
          </div>
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
          <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
            Retrieved context
          </div>
          <div className="flex flex-wrap gap-1.5">
            {draft.retrieved_context_ids.map((id) => (
              <span
                key={id}
                title={id}
                className="max-w-full break-all rounded-full bg-indigo-50 px-2 py-0.5 font-mono text-[11px] text-indigo-700"
              >
                {shortenCaseId(id)}
              </span>
            ))}
          </div>
        </div>
      )}

      {unavailable.length > 0 && (
        <div className="rounded-md bg-amber-50 px-2 py-1.5 text-xs text-amber-800">
          {unavailable.length} evidence item{unavailable.length === 1 ? '' : 's'} marked unavailable.
        </div>
      )}
    </section>
  );
}

// Group followup events into alternating runs of "messages" (always
// visible) and "tools" (collapsible). Orphan tool_error events (no
// preceding tool_call — these are turn-level diagnostics like budget
// exhaustion) stay with messages so the user never loses sight of an
// error. The first event's seq is used as a stable identity for the
// run, used both as a React key and as the expanded-set key.
type FollowupGroup =
  | { kind: 'messages'; firstSeq: number; events: AgentTraceEvent[] }
  | { kind: 'tools'; firstSeq: number; events: AgentTraceEvent[] };

function groupFollowupEvents(events: AgentTraceEvent[]): FollowupGroup[] {
  const groups: FollowupGroup[] = [];
  for (const event of events) {
    const isToolEvent =
      event.type === 'tool_call' || event.type === 'tool_result' || event.type === 'tool_error';
    // A tool_error with no name is an orphan diagnostic (e.g. "agent
    // stopped without calling propose_eval_case"). Keep it inline with
    // messages so the user always sees it.
    const isOrphanToolError = event.type === 'tool_error' && !event.name;
    const targetKind: FollowupGroup['kind'] =
      isToolEvent && !isOrphanToolError ? 'tools' : 'messages';
    const last = groups[groups.length - 1];
    const lastTurn = last ? last.events[last.events.length - 1].turn : null;
    // Break on turn boundary too — even if the kind matches, an
    // event from a different turn starts a new group. The verdict
    // block needs to slot in cleanly between turns, so each group
    // must belong to exactly one turn.
    if (last && last.kind === targetKind && lastTurn === event.turn) {
      last.events.push(event);
    } else {
      groups.push({ kind: targetKind, firstSeq: event.seq, events: [event] });
    }
  }
  return groups;
}

function FollowupTimeline({
  events,
  pendingUserMessage,
  inFlight,
  panelError,
  expandedEvents,
  onToggleEvent,
  onSelectStep,
  runId,
  expandedRuns,
  onToggleRun,
  verdictBlock,
  verdictAfterTurn,
  streamingMessages,
}: {
  events: AgentTraceEvent[];
  pendingUserMessage: string | null;
  inFlight: boolean;
  panelError: string | null;
  expandedEvents: Set<number>;
  onToggleEvent: (seq: number) => void;
  onSelectStep: (index: number) => void;
  runId: string | null;
  expandedRuns: Set<number>;
  onToggleRun: (firstSeq: number) => void;
  // The verdict trio (Analysis Result + draft editor) follows whichever
  // turn produced the latest propose_eval_case. The parent passes the
  // pre-built node + which turn to anchor it after. null means the
  // verdict belongs to turn 0 (rendered by the parent above this
  // timeline) or doesn't exist yet.
  verdictBlock: ReactNode;
  verdictAfterTurn: number | null;
  // In-flight streaming text per LLM generation. Each entry is
  // rendered as a left-aligned bubble at the end of its originating
  // turn — typewriter-style typing while OpenAI streams.
  streamingMessages: Map<string, StreamingMessage>;
}) {
  const groups = useMemo(() => groupFollowupEvents(events), [events]);

  // Group streaming bubbles by turn so we can render them next to the
  // right message cluster. Multiple streams per turn are rare (one
  // turn = one LLM call typically) but supported — Map iteration
  // preserves insertion order, matching arrival order.
  const streamingByTurn = useMemo(() => {
    const byTurn = new Map<number, Array<{ id: string; text: string }>>();
    for (const [id, entry] of streamingMessages.entries()) {
      if (!entry.text) continue;
      const list = byTurn.get(entry.turn) ?? [];
      list.push({ id, text: entry.text });
      byTurn.set(entry.turn, list);
    }
    return byTurn;
  }, [streamingMessages]);

  // Last tool group is auto-expanded while the stream is in-flight so the
  // user sees the agent's tool work live. Other completed tool groups
  // collapse by default and require an explicit click to expand.
  const lastToolGroupIndex = useMemo(() => {
    for (let i = groups.length - 1; i >= 0; i -= 1) {
      if (groups[i].kind === 'tools') return i;
    }
    return -1;
  }, [groups]);

  const hasContent =
    groups.length > 0 || Boolean(pendingUserMessage) || inFlight || Boolean(panelError);
  if (!hasContent) return null;

  return (
    <section className="space-y-2">
      {groups.map((group, index) => {
        const groupTurn = group.events[0]?.turn ?? 0;
        const nextGroup = groups[index + 1];
        const nextTurn = nextGroup ? nextGroup.events[0]?.turn ?? null : null;
        // True when this is the last group belonging to its turn — i.e.
        // the next group exists in a different turn, or there is no
        // next group. Used to decide where to drop the verdict block so
        // it appears at the *end* of the producing turn rather than
        // immediately after the propose_eval_case tool call.
        const isLastGroupInTurn = nextTurn === null || nextTurn !== groupTurn;
        const shouldRenderVerdictAfter =
          verdictBlock !== null && verdictAfterTurn === groupTurn && isLastGroupInTurn;

        let groupNode: ReactNode;
        if (group.kind === 'messages') {
          groupNode = (
            <TraceHistory
              events={group.events}
              pendingUserMessage={null}
              inFlight={false}
              panelError={null}
              expandedEvents={expandedEvents}
              onToggleEvent={onToggleEvent}
              onSelectStep={onSelectStep}
              runId={runId}
            />
          );
        } else {
          const isStreamingGroup = inFlight && index === lastToolGroupIndex;
          const expanded = isStreamingGroup || expandedRuns.has(group.firstSeq);
          groupNode = (
            <>
              {!isStreamingGroup && (
                <FollowupToolRunToggle
                  collapsed={!expanded}
                  events={group.events}
                  onToggle={() => onToggleRun(group.firstSeq)}
                />
              )}
              {expanded && (
                <TraceHistory
                  events={group.events}
                  pendingUserMessage={null}
                  inFlight={isStreamingGroup}
                  panelError={null}
                  expandedEvents={expandedEvents}
                  onToggleEvent={onToggleEvent}
                  onSelectStep={onSelectStep}
                  runId={runId}
                />
              )}
            </>
          );
        }
        const turnStreams = isLastGroupInTurn ? streamingByTurn.get(groupTurn) ?? [] : [];
        return (
          <Fragment key={`g-${group.firstSeq}`}>
            {groupNode}
            {turnStreams.map((stream) => (
              <MessageBubble key={`stream-${stream.id}`} align="left" message={stream.text} />
            ))}
            {shouldRenderVerdictAfter && verdictBlock}
          </Fragment>
        );
      })}

      {pendingUserMessage && <MessageBubble align="right" message={pendingUserMessage} muted />}
      {panelError && (
        <div className="rounded-md border border-red-200 bg-red-50 p-2 text-xs text-red-700">
          {panelError}
        </div>
      )}
    </section>
  );
}

function FollowupToolRunToggle({
  collapsed,
  events,
  onToggle,
}: {
  collapsed: boolean;
  events: AgentTraceEvent[];
  onToggle: () => void;
}) {
  const toolCallCount = events.filter((event) => event.type === 'tool_call').length;
  const label = `${toolCallCount} tool call${toolCallCount === 1 ? '' : 's'}`;
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center gap-2 text-left text-[11px] text-slate-500 hover:text-indigo-600"
      title={collapsed ? 'Expand tool calls' : 'Collapse tool calls'}
    >
      <svg className="h-3.5 w-3.5 shrink-0 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.2" d="M5 12l5 5L20 7" />
      </svg>
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="shrink-0 text-slate-400">{collapsed ? '▾ Show' : '▴ Hide'}</span>
    </button>
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
  get_run: 'Trajectory metadata lookup',
  get_step_detail: 'Step detail inspection',
  search_failure_memory: 'Failure patterns retrieval',
  search_eval_cases: 'Verified verdict retrieval',
  find_similar_successful_run: 'Similar successful trajectories retrieval',
  propose_eval_case: 'Verdict proposal',
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
            <DetailRow label="Trajectories" stacked>
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
        <span>{digest.preprocess_version}</span>
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
      return runId ? `Loaded trajectory metadata for ${shortRunId(runId)}` : 'Loaded trajectory metadata';
    case 'get_step_detail':
      return stepIndex !== null
        ? `Inspected step ${stepIndex}`
        : 'Inspected a step';
    case 'find_similar_successful_run':
      if (itemCount === 0) return 'Looked for similar successful trajectories — none yet';
      if (itemCount !== null) return `Found ${itemCount} similar successful ${itemCount === 1 ? 'trajectory' : 'trajectories'}`;
      return task ? `Searching for similar successful trajectories to: ${shorten(task, 60)}` : 'Searching for similar successful trajectories';
    case 'search_failure_memory':
      if (itemCount !== null && query) return `Retrieved failure patterns for "${shorten(query, 60)}" — ${itemCount} match${itemCount === 1 ? '' : 'es'}`;
      if (query) return `Retrieving failure patterns for "${shorten(query, 60)}"`;
      return 'Retrieving failure patterns';
    case 'search_eval_cases':
      if (itemCount !== null && query) return `Retrieved verified verdicts for "${shorten(query, 60)}" — ${itemCount} match${itemCount === 1 ? '' : 'es'}`;
      if (query) return `Retrieving verified verdicts for "${shorten(query, 60)}"`;
      return 'Retrieving verified verdicts';
    case 'propose_eval_case': {
      const caseId = typeof result?.case_id === 'string' ? result.case_id : null;
      const isSuccess = result?.failure_type === null || result?.failure_type === undefined;
      if (caseId && isSuccess) return `Drafted success verdict (${shortenCaseId(caseId)})`;
      if (caseId) return `Drafted verdict (${shortenCaseId(caseId)})`;
      return 'Drafting verdict';
    }
    default:
      return event.name ?? '(unknown tool)';
  }
}

function shortRunId(runId: string) {
  return runId.length > 12 ? `${runId.slice(0, 8)}…` : runId;
}

function MessageBubble({ align, message, muted = false }: { align: 'left' | 'right'; message: string; muted?: boolean }) {
  const empty = !message;
  // overflow-wrap:anywhere lets unbreakable tokens (long hashes like
  // ec_<64-char>, run_ids, URLs) wrap mid-token instead of pushing
  // past the bubble's max-w. Cascades to every descendant
  // <strong>/<code>/<li>/<a> inside AgentMarkdown so the markdown
  // renderer doesn't need per-element fixes.
  return (
    <div className={`flex min-w-0 ${align === 'right' ? 'justify-end' : 'justify-start'}`}>
      <div
        className={`max-w-[85%] min-w-0 rounded-lg px-3 py-2 text-sm leading-5 [overflow-wrap:anywhere] ${
          align === 'right'
            ? 'bg-indigo-600 text-white'
            : 'border border-slate-200 bg-white text-slate-700'
        } ${muted ? 'opacity-70' : ''}`}
      >
        {empty ? '(empty message)' : align === 'left' ? <AgentMarkdown source={message} /> : message}
      </div>
    </div>
  );
}

// Agent messages frequently carry Markdown (bullets, **bold**, `code`,
// occasional code fences). Rendering them as plain text leaks the raw
// asterisks/backticks into the UI. Streamdown is a react-markdown
// drop-in that auto-closes unterminated emphasis/code-fence tokens
// (`**bold` → renders as **bold** while the closing `**` is still
// streaming), which removes the typewriter jitter when we move to real
// token streaming. For already-complete agent_message events it
// behaves identically to react-markdown, so wiring it now costs nothing
// and removes a future migration step.
//
// Component overrides remain pinned to Tailwind classes so output stays
// inside the bubble's typography budget. Raw HTML is NOT enabled
// (Streamdown's default sanitization is on) — agent-produced `<script>`
// tags still render as literal text.
function AgentMarkdown({ source }: { source: string }) {
  return (
    <div className="space-y-2">
      <Streamdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="leading-5">{children}</p>,
          ul: ({ children }) => <ul className="list-disc space-y-1 pl-5 leading-5">{children}</ul>,
          ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5 leading-5">{children}</ol>,
          li: ({ children }) => <li className="leading-5">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold text-slate-900">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          a: ({ href, children }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" className="text-indigo-600 underline hover:text-indigo-700">
              {children}
            </a>
          ),
          code: ({ children, className }) => {
            // remark passes `className="language-xxx"` for fenced code blocks
            // and no className for inline code. Use that to switch presentation.
            const isBlock = Boolean(className);
            if (isBlock) {
              return (
                <code className={`block w-full ${className ?? ''}`}>{children}</code>
              );
            }
            return (
              <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[12px] text-slate-700">{children}</code>
            );
          },
          pre: ({ children }) => (
            <pre className="max-h-64 overflow-auto rounded-md bg-slate-100 p-2 font-mono text-[11px] leading-4 text-slate-700">
              {children}
            </pre>
          ),
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-slate-300 pl-3 text-slate-600">{children}</blockquote>
          ),
          h1: ({ children }) => <h4 className="text-sm font-bold text-slate-900">{children}</h4>,
          h2: ({ children }) => <h4 className="text-sm font-bold text-slate-900">{children}</h4>,
          h3: ({ children }) => <h4 className="text-sm font-semibold text-slate-900">{children}</h4>,
          h4: ({ children }) => <h4 className="text-sm font-semibold text-slate-900">{children}</h4>,
          h5: ({ children }) => <h5 className="text-xs font-semibold text-slate-900">{children}</h5>,
          h6: ({ children }) => <h6 className="text-xs font-semibold text-slate-900">{children}</h6>,
          hr: () => <hr className="border-slate-200" />,
          table: ({ children }) => (
            <div className="overflow-x-auto">
              <table className="min-w-full border-collapse text-[12px]">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border-b border-slate-200 px-2 py-1 text-left font-semibold text-slate-700">{children}</th>
          ),
          td: ({ children }) => <td className="border-b border-slate-100 px-2 py-1 align-top">{children}</td>,
        }}
      >
        {source}
      </Streamdown>
    </div>
  );
}

function EvalCaseDraftPanel({
  draft,
  onDraftChange,
  onSelectStep,
  onValidated,
  onClose,
}: {
  draft: EvalCase | null;
  onDraftChange: (draft: EvalCase | null) => void;
  onSelectStep: (index: number) => void;
  onValidated?: () => void;
  onClose?: () => void;
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
    // Echo of our own edit: every local `update()` calls onDraftChange,
    // the parent re-renders, and the same value comes back here as the
    // `draft` prop. Without this guard the effect would run end-to-end
    // — including the lastFailureFieldsRef reset — and wipe the
    // success/failure-toggle stash one tick after setMode populated it,
    // which is why the failure fields kept disappearing on toggle-back.
    if (JSON.stringify(draft) === JSON.stringify(localDraftRef.current)) {
      return;
    }
    if (dirtyRef.current) {
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

  const deleteEvidence = (index: number) => {
    const evidence = localDraft.evidence.filter((_, itemIndex) => itemIndex !== index);
    update({ evidence });
  };

  const removeContextId = (id: string) => {
    update({ retrieved_context_ids: localDraft.retrieved_context_ids.filter((entry) => entry !== id) });
  };

  const addContextId = (id: string) => {
    const value = id.trim();
    if (!value || localDraft.retrieved_context_ids.includes(value)) return;
    update({ retrieved_context_ids: [...localDraft.retrieved_context_ids, value] });
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
      setExportStatus('Saved. Indexed into RAG; trajectory status updated.');
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
    <DraftPanelBody
      localDraft={localDraft}
      dirty={dirty}
      exportStatus={exportStatus}
      isSuccess={isSuccess}
      onClose={onClose}
      onSelectStep={onSelectStep}
      onUpdate={update}
      onSetMode={setMode}
      onUpdateEvidenceClaim={updateEvidenceClaim}
      onDeleteEvidence={deleteEvidence}
      onAddContextId={addContextId}
      onRemoveContextId={removeContextId}
      onSave={saveDraft}
    />
  );
}

function DraftPanelBody({
  localDraft,
  dirty,
  exportStatus,
  isSuccess,
  onClose,
  onSelectStep,
  onUpdate,
  onSetMode,
  onUpdateEvidenceClaim,
  onDeleteEvidence,
  onAddContextId,
  onRemoveContextId,
  onSave,
}: {
  localDraft: EvalCase;
  dirty: boolean;
  exportStatus: string | null;
  isSuccess: boolean;
  onClose?: () => void;
  onSelectStep: (index: number) => void;
  onUpdate: (patch: Partial<EvalCase>) => void;
  onSetMode: (toSuccess: boolean) => void;
  onUpdateEvidenceClaim: (index: number, claim: string) => void;
  onDeleteEvidence: (index: number) => void;
  onAddContextId: (id: string) => void;
  onRemoveContextId: (id: string) => void;
  onSave: () => void;
}) {
  // Inline "+ add" form state for retrieved_context_ids. Stays local
  // to this body component — closed/reopened modal preserves
  // localDraft via the parent's always-mounted approach; the
  // ephemeral "I'm typing a new context id" state is fine to drop
  // when the user dismisses (rare workflow anyway).
  const [addingContext, setAddingContext] = useState(false);
  const [newContextValue, setNewContextValue] = useState('');

  const commitNewContext = () => {
    const value = newContextValue.trim();
    if (value) onAddContextId(value);
    setNewContextValue('');
    setAddingContext(false);
  };

  return (
    <>
      {/* Modal header */}
      <div className="flex shrink-0 items-center justify-between border-b border-slate-100 px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="text-sm font-bold text-slate-900">Draft Verdict</h3>
          {dirty && <span className="text-[10px] text-amber-600">· edited</span>}
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
            title="Close (Esc)"
            aria-label="Close"
          >
            <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 6l12 12M6 18L18 6" />
            </svg>
          </button>
        )}
      </div>

      {/* Scrollable body */}
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 text-xs text-slate-700">
        {/* Compact metadata strip — hashes don't deserve their own slot */}
        <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[10px] font-mono text-slate-500">
          <span title={localDraft.case_id} className="break-all">
            <span className="mr-1 text-[9px] uppercase tracking-wider text-slate-400">case</span>
            {shortenCaseId(localDraft.case_id)}
          </span>
          <span title={localDraft.source_run_id} className="break-all">
            <span className="mr-1 text-[9px] uppercase tracking-wider text-slate-400">trajectory</span>
            {shortRunId(localDraft.source_run_id)}
          </span>
        </div>

        {/* Mode toggle (failure / success XOR) */}
        <div className="mb-3 flex gap-1 rounded-md border border-slate-200 bg-slate-50 p-1">
          <ModeButton active={!isSuccess} onClick={() => onSetMode(false)} tone="failure">Failure case</ModeButton>
          <ModeButton active={isSuccess} onClick={() => onSetMode(true)} tone="success">Success case</ModeButton>
        </div>

        {isSuccess ? (
          <div className="mb-4 rounded-md bg-emerald-50 px-3 py-2 text-[11px] leading-5 text-emerald-800">
            <div className="mb-0.5 text-[9px] font-semibold uppercase tracking-wider text-emerald-700">Verdict</div>
            Success — validating this draft marks the trajectory successful and indexes it for similar-success retrieval.
          </div>
        ) : (
          <FailureFieldsBlock localDraft={localDraft} onUpdate={onUpdate} />
        )}

        {/* Evidence — each item is a light row, not a bordered card */}
        <div className="mb-4">
          <div className="mb-2 text-[9px] font-semibold uppercase tracking-wider text-slate-500">Evidence</div>
          {localDraft.evidence.length === 0 ? (
            <p className="text-[11px] italic text-slate-400">No evidence — the agent did not attach any items.</p>
          ) : (
            <ul className="space-y-2.5 text-[12px] leading-5 text-slate-700">
              {localDraft.evidence.map((item, index) => (
                <li
                  key={`${item.source}-${index}-${item.claim.slice(0, 16)}`}
                  className="group relative pl-4 before:absolute before:left-0 before:top-2 before:h-1 before:w-1 before:rounded-full before:bg-slate-400"
                >
                  {/* Borderless textarea masquerading as plain text;
                      focus reveals a soft slate-100 background so the
                      user knows it's editable. AutoGrowTextarea sizes
                      the element to scrollHeight on every value
                      change so the full claim is always visible
                      without an inner scrollbar. */}
                  <AutoGrowTextarea
                    value={item.claim}
                    onChange={(value) => onUpdateEvidenceClaim(index, value)}
                    className="block w-full resize-none overflow-hidden break-words border-0 bg-transparent p-0 text-[12px] leading-5 text-slate-700 outline-none focus:rounded focus:bg-slate-100/60"
                  />
                  <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-[10px]">
                    <span className="rounded bg-slate-100 px-1.5 font-mono text-slate-600">{item.source}</span>
                    {typeof item.step_index === 'number' && (
                      <>
                        <span className="text-slate-400">·</span>
                        <button
                          type="button"
                          onClick={() => onSelectStep(item.step_index as number)}
                          className="text-slate-500 hover:text-indigo-700"
                        >
                          step {item.step_index}
                        </button>
                      </>
                    )}
                    {item.context_id && (
                      <>
                        <span className="text-slate-400">·</span>
                        <code className="font-mono text-indigo-700">{shortenCaseId(item.context_id)}</code>
                      </>
                    )}
                    <span className="ml-auto flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                      <button
                        type="button"
                        onClick={() => onDeleteEvidence(index)}
                        className="rounded p-0.5 text-slate-400 hover:bg-red-50 hover:text-red-600"
                        title="Remove this evidence item"
                        aria-label="Remove evidence"
                      >
                        <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" d="M6 6l12 12M6 18L18 6" />
                        </svg>
                      </button>
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Retrieved context chips */}
        <div>
          <div className="mb-2 text-[9px] font-semibold uppercase tracking-wider text-slate-500">Retrieved context</div>
          <div className="flex flex-wrap items-center gap-1.5">
            {localDraft.retrieved_context_ids.map((id) => (
              <span
                key={id}
                title={id}
                className="inline-flex max-w-full items-center gap-1 break-all rounded-full bg-indigo-50 px-2 py-0.5 font-mono text-[11px] text-indigo-700"
              >
                {shortenCaseId(id)}
                <button
                  type="button"
                  onClick={() => onRemoveContextId(id)}
                  className="text-indigo-400 hover:text-red-600"
                  title="Remove this context id"
                  aria-label="Remove context"
                >
                  ×
                </button>
              </span>
            ))}
            {addingContext ? (
              <input
                autoFocus
                value={newContextValue}
                onChange={(event) => setNewContextValue(event.target.value)}
                onBlur={commitNewContext}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    commitNewContext();
                  } else if (event.key === 'Escape') {
                    event.preventDefault();
                    setNewContextValue('');
                    setAddingContext(false);
                  }
                }}
                placeholder="fm_… or ec_…"
                className="rounded-full border border-indigo-200 bg-white px-2 py-0.5 font-mono text-[11px] text-indigo-700 outline-none placeholder:text-slate-300 focus:border-indigo-400"
              />
            ) : (
              <button
                type="button"
                onClick={() => setAddingContext(true)}
                className="rounded-full border border-dashed border-slate-300 px-2 py-0.5 text-[11px] text-slate-500 hover:bg-slate-50"
              >
                + add
              </button>
            )}
          </div>
        </div>

        {exportStatus && <div className="mt-3 text-[11px] text-slate-500">{exportStatus}</div>}
      </div>

      {/* Sticky footer: validate + cancel + save together */}
      <div className="flex shrink-0 items-center justify-between gap-2 border-t border-slate-100 bg-slate-50/60 px-4 py-3">
        <label className="flex items-center gap-1.5 text-[12px] text-slate-700">
          <input
            type="checkbox"
            checked={localDraft.human_validated}
            onChange={(event) => onUpdate({ human_validated: event.target.checked })}
            className="h-3.5 w-3.5 rounded border-slate-300"
          />
          Mark validated
        </label>
        <div className="flex items-center gap-2">
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              className="rounded-md border border-slate-200 bg-white px-3 py-1.5 text-[12px] font-semibold text-slate-700 hover:bg-slate-50"
            >
              Cancel
            </button>
          )}
          <button
            type="button"
            onClick={onSave}
            disabled={!localDraft.human_validated}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-[12px] font-semibold text-white hover:bg-indigo-700 disabled:cursor-not-allowed disabled:opacity-50"
            title="Persist the validated EvalCase, flip the source trajectory's status, and index into ChromaDB for RAG."
          >
            Save
          </button>
        </div>
      </div>
    </>
  );
}

function FailureFieldsBlock({
  localDraft,
  onUpdate,
}: {
  localDraft: EvalCase;
  onUpdate: (patch: Partial<EvalCase>) => void;
}) {
  return (
    <>
      {/* Failure step + type side-by-side */}
      <div className="mb-2 grid grid-cols-2 gap-2">
        <div className="rounded-md bg-slate-100/70 px-3 py-2">
          <div className="mb-0.5 text-[9px] font-semibold uppercase tracking-wider text-slate-500">Failure step</div>
          <input
            type="number"
            value={typeof localDraft.failure_step === 'number' ? localDraft.failure_step : 1}
            onChange={(event) => onUpdate({ failure_step: Number(event.target.value) })}
            className="w-full border-0 bg-transparent p-0 text-[13px] font-semibold text-slate-900 outline-none focus:ring-0"
          />
        </div>
        <div className="rounded-md bg-slate-100/70 px-3 py-2">
          <div className="mb-0.5 text-[9px] font-semibold uppercase tracking-wider text-slate-500">Failure type</div>
          <input
            value={localDraft.failure_type ?? ''}
            onChange={(event) => onUpdate({ failure_type: event.target.value })}
            placeholder="early_terminated"
            className="w-full border-0 bg-transparent p-0 font-mono text-[12px] text-red-700 outline-none placeholder:text-slate-300 focus:ring-0"
          />
        </div>
      </div>

      {/* Tinted-block textareas for the long fields */}
      <TintedTextField
        label="Expected behavior"
        value={localDraft.expected_behavior ?? ''}
        onChange={(value) => onUpdate({ expected_behavior: value })}
      />
      <TintedTextField
        label="Observed behavior"
        value={localDraft.actual_behavior ?? ''}
        onChange={(value) => onUpdate({ actual_behavior: value })}
      />
      <TintedTextField
        label="Regression rule"
        value={localDraft.regression_rule ?? ''}
        onChange={(value) => onUpdate({ regression_rule: value })}
      />
    </>
  );
}

function AutoGrowTextarea({
  value,
  onChange,
  className,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  className?: string;
  placeholder?: string;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);

  // Resize on every value change so multi-line content fits exactly
  // without an inner scrollbar. height='auto' first lets the
  // textarea shrink when content is deleted; scrollHeight then
  // grows it to fit. overflow-hidden is applied at the call site so
  // any one-tick mismatch doesn't flash a scrollbar.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${el.scrollHeight}px`;
  }, [value]);

  // Re-measure when the element first becomes visible. The verdict
  // modal pre-mounts its children while hidden via display:none so
  // that closing it is non-destructive — but a hidden element has
  // scrollHeight=0, which would otherwise lock the textarea to a
  // zero-height row even after the modal opens. IntersectionObserver
  // fires when the element enters the viewport (which happens on the
  // display:none → display:flex transition), giving us a clean place
  // to remeasure once the textarea actually has layout.
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const remeasure = () => {
      el.style.height = 'auto';
      el.style.height = `${el.scrollHeight}px`;
    };
    if (typeof IntersectionObserver === 'undefined') {
      remeasure();
      return undefined;
    }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        remeasure();
      }
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <textarea
      ref={ref}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      rows={1}
      placeholder={placeholder}
      className={className}
    />
  );
}

function TintedTextField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <div className="mb-2 rounded-md bg-slate-100/70 px-3 py-2">
      <div className="mb-0.5 text-[9px] font-semibold uppercase tracking-wider text-slate-500">{label}</div>
      <textarea
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={2}
        className="block w-full resize-y border-0 bg-transparent p-0 text-xs leading-5 text-slate-700 outline-none focus:ring-0"
      />
    </div>
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

function TerminationBadge({ trace, latestToolError, inFlight }: { trace: AgentTrace | null; latestToolError: string | null; inFlight: boolean }) {
  // In the compact single-row header the AnalyzeButton itself communicates
  // "no trace yet" (idle state) and "running…" (inFlight). The termination
  // badge only adds value once a trace has actually terminated, so we
  // render nothing in those transient cases.
  if (!trace || inFlight) return null;
  // Skip the emptyTrace placeholder ("error" baked in before the real
  // `done` event lands) — only show once a real event has been recorded.
  if (trace.events.length === 0) return null;
  const classes = trace.terminated_by === 'propose_eval_case'
    ? 'bg-emerald-50 text-emerald-700'
    : trace.terminated_by === 'budget_exceeded'
      ? 'bg-slate-100 text-slate-600'
      : 'bg-red-50 text-red-700';
  // Raw snake_case terminators are internal contract values (mirrored in
  // docs/eval_agent.md). Translate to human-readable labels for the UI;
  // hover title still carries the underlying tool error when present.
  const label = trace.terminated_by === 'propose_eval_case'
    ? 'Analysis complete'
    : trace.terminated_by === 'budget_exceeded'
      ? 'Budget exceeded'
      : 'Error';
  return (
    <div>
      <span title={trace.terminated_by === 'error' ? latestToolError ?? undefined : undefined} className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${classes}`}>
        {label}
      </span>
    </div>
  );
}

function TraceCollapseToggle({
  collapsed,
  events,
  runtimeMs,
  onToggle,
}: {
  collapsed: boolean;
  events: AgentTraceEvent[];
  runtimeMs: number;
  onToggle: () => void;
}) {
  const toolCallCount = events.filter((event) => event.type === 'tool_call').length;
  const label = `${toolCallCount} tool call${toolCallCount === 1 ? '' : 's'}`;
  const runtime = runtimeMs > 0 ? ` · ${formatRuntime(runtimeMs)}` : '';
  // Plain-text affordance, no bordered card. Once the verdict is the
  // visual focus, the timeline summary should fade — user opens it on
  // demand. The hover state lifts the text color to indigo so it still
  // reads as interactive.
  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex w-full items-center gap-2 text-left text-[11px] text-slate-500 hover:text-indigo-600"
      title={collapsed ? 'Expand tool-call timeline' : 'Collapse tool-call timeline'}
    >
      <svg className="h-3.5 w-3.5 shrink-0 text-emerald-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.2" d="M5 12l5 5L20 7" />
      </svg>
      <span className="min-w-0 flex-1 truncate">{label}{runtime}</span>
      <span className="shrink-0 text-slate-400">{collapsed ? '▾ Show timeline' : '▴ Hide timeline'}</span>
    </button>
  );
}

function TraceFooter({ trace, inFlight }: { trace: AgentTrace | null; inFlight: boolean }) {
  // Live wall-clock while the agent is streaming. The backend's
  // authoritative runtime_ms only lands once the stream's `done` event
  // arrives, so we tick a client-side timer in the meantime — same UX
  // pattern as Claude Code's "5.2s" running clock under the message.
  const [liveRuntime, setLiveRuntime] = useState(0);
  useEffect(() => {
    if (!inFlight) return undefined;
    setLiveRuntime(0);
    const start = Date.now();
    const interval = window.setInterval(() => setLiveRuntime(Date.now() - start), 250);
    return () => window.clearInterval(interval);
  }, [inFlight]);

  // Read the LATEST turn's metrics, not the trace cumulative totals.
  // Cumulative kept growing with every followup, but the user wants
  // "this exchange just cost X" — same UX as Claude Code's per-turn
  // counter.
  const latestTurn = trace?.turn_metrics?.[trace.turn_metrics.length - 1] ?? null;
  const finalRuntime = latestTurn?.runtime_ms ?? trace?.runtime_ms ?? 0;
  const runtime = inFlight ? liveRuntime : finalRuntime;
  const inputTokens = latestTurn?.input_tokens ?? trace?.input_tokens ?? 0;
  const outputTokens = latestTurn?.output_tokens ?? trace?.output_tokens ?? 0;
  const hasTokens = inputTokens > 0 || outputTokens > 0;
  if (runtime <= 0 && !hasTokens && !inFlight) return null;

  const parts: string[] = [];
  if (runtime > 0 || inFlight) parts.push(formatRuntime(runtime));
  // Tokens are only known once the stream's `done` event delivers the
  // final trace. During streaming we show a soft placeholder so the
  // footer's width doesn't snap when the numbers land. "tokens" suffix
  // is appended once to the last token segment so the unit is obvious
  // without repeating "tok" twice.
  if (hasTokens) {
    parts.push(`${formatTokens(inputTokens)} in`);
    parts.push(`${formatTokens(outputTokens)} out tokens`);
  } else if (inFlight) {
    parts.push('counting tokens…');
  }

  return (
    <div
      className="flex items-center gap-1.5 px-1 pt-1 text-[10px] text-slate-500"
      title={
        `Latest turn runtime: ${runtime} ms · LLM tokens (VLM not counted): `
        + `${inputTokens.toLocaleString()} in / ${outputTokens.toLocaleString()} out`
      }
    >
      <svg className="h-3 w-3 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <circle cx="12" cy="13" r="8" strokeWidth="1.8" />
        <path d="M12 9v4l2 2M12 3v2" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
      <span className="font-mono">{parts.join(' · ')}</span>
    </div>
  );
}

function formatRuntime(ms: number): string {
  // Whole-second granularity only: sub-second jitter and "234 ms"
  // readings aren't useful to the user. Round to nearest second; 0 ms
  // stays 0s so the footer's hide-when-zero check still works.
  if (ms <= 0) return '0s';
  const seconds = Math.round(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return `${minutes}m ${remainder}s`;
}

function formatTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 10_000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n / 1000)}k`;
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
    { label: 'Suggest failure label', text: 'Suggest the failure label for this trajectory.' },
    { label: 'Generate verdict', text: 'Generate the draft verdict.' },
    { label: 'Find similar failures', text: 'Find similar failure cases from memory.' },
    { label: 'Compare with another trajectory', text: 'Compare this trajectory with a similar successful trajectory.' },
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
      text: 'This trajectory actually succeeded. Please re-propose the verdict as a success case (clear all failure fields).',
    },
    {
      label: 'Reclassify as failure',
      text: 'This trajectory actually failed. Please re-propose the verdict with the correct failure step, failure type, expected behavior, and actual behavior.',
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
    runtime_ms: 0,
    input_tokens: 0,
    output_tokens: 0,
    turn_metrics: [],
  };
}

function appendEvent(runId: string, event: AgentTraceEvent) {
  return (current: AgentTrace | null): AgentTrace => {
    const base = current ?? emptyTrace(runId);
    if (base.events.some((item) => item.seq === event.seq)) return base;
    return { ...base, events: [...base.events, event] };
  };
}

// Streaming bubble helpers — kept as plain functions (not hooks) so
// they can be inlined in onDelta/onEvent without adding a dep on the
// component's state setters.

function appendDelta(
  current: Map<string, StreamingMessage>,
  delta: AgentDelta,
): Map<string, StreamingMessage> {
  const next = new Map(current);
  const existing = next.get(delta.stream_id);
  next.set(delta.stream_id, {
    turn: existing?.turn ?? delta.turn,
    text: (existing?.text ?? '') + delta.text,
  });
  return next;
}

function dropFinalizedStream(
  current: Map<string, StreamingMessage>,
  finalMessage: string,
): Map<string, StreamingMessage> {
  // The persisted agent_message event has the full content. Drop any
  // streaming entry whose accumulated text matches (the same LLM
  // generation just landed in the trace), so the bubble doesn't
  // render twice during the small finalization window. If nothing
  // matches exactly we still leave the entry; it'll get cleared on
  // inFlight=false anyway.
  let changed = false;
  const next = new Map<string, StreamingMessage>();
  for (const [key, value] of current.entries()) {
    if (value.text === finalMessage) {
      changed = true;
      continue;
    }
    next.set(key, value);
  }
  return changed ? next : current;
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
