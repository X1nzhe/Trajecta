// frontend/src/components/EvalAgentPanel.tsx — PARTIAL PATCH
// Replace ONLY the `ObservationSummaryPanel` function inside the
// existing EvalAgentPanel.tsx with the version below. Don't touch
// any of the surrounding stream / draft / followup logic.
//
// What changes visually:
//   1. Eyebrow labels become smaller uppercase + monospace weight.
//   2. Evidence list switches from grey bullets to numbered mono indices
//      (01, 02, …) — much more "trace report" than "marketing page".
//   3. The "Expected behavior" block becomes a quieter divider-led section
//      instead of a tinted card (less visual noise stacked on the analysis).
//   4. Step pill in the header gets a leading dot to match the run-list pills.
//   5. Failure label uses the canvas hairline / mono font tokens.
//
// Behavior, props, and child references (onSelectStep / onOpenTraceEvent)
// are unchanged.

import type { EvidenceItem, EvalCase, TrajectoryRun, AgentTrace } from '../types/contracts';

// `isVisualEvidence` and `shortenCaseId` are existing helpers in
// EvalAgentPanel.tsx — keep using them, don't redefine.

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
        <Eyebrow>Analysis result</Eyebrow>
        <p className="mt-2 text-sm leading-5 text-slate-500">
          No findings yet. Run an analysis to produce a trace and draft verdict.
        </p>
      </section>
    );
  }

  const stale = trace?.terminated_by && trace.terminated_by !== 'propose_eval_case';
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
  const displayStep = typeof draft.failure_step === 'number' ? draft.failure_step : null;

  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <Eyebrow>Analysis result</Eyebrow>
        <div className="flex items-center gap-1.5">
          {stale && (
            <span className="rounded bg-amber-50 px-1.5 py-0.5 font-mono text-[10px] font-semibold text-amber-700">
              stale
            </span>
          )}
          {!isSuccess && displayStep !== null && typeof draft.failure_step === 'number' && (
            <button
              onClick={() => onSelectStep(draft.failure_step as number)}
              className="inline-flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[11px] font-semibold text-red-700 hover:bg-red-100"
              title="Jump to the step the agent attributed failure to"
            >
              <span className="h-1.5 w-1.5 rounded-full bg-red-500" />
              step <span className="font-mono tabular-nums">{displayStep}</span>
              <span aria-hidden="true">↗</span>
            </button>
          )}
        </div>
      </div>

      {/* Primary headline: actual_behavior promoted to the visual anchor */}
      {draft.actual_behavior && (
        <p className="break-words text-[15px] font-medium leading-[1.5] text-slate-900">
          {draft.actual_behavior}
        </p>
      )}
      {isSuccess && (
        <p className="break-words text-[15px] font-medium leading-[1.5] text-emerald-700">
          The agent concluded this trajectory completed the task successfully.
        </p>
      )}

      {/* Expected behavior — quiet divider-led section, no tinted card */}
      {draft.expected_behavior && (
        <div className="border-t border-[color:var(--color-hairline)] pt-3">
          <Eyebrow>Expected behavior</Eyebrow>
          <p className="mt-1.5 break-words text-[12.5px] leading-5 text-slate-600">
            {draft.expected_behavior}
          </p>
        </div>
      )}

      {/* Supporting evidence — numbered mono indices */}
      {draft.evidence.length > 0 && (
        <div className="border-t border-[color:var(--color-hairline)] pt-3">
          <div className="mb-2 flex items-center justify-between">
            <Eyebrow>Supporting evidence</Eyebrow>
            <span className="font-mono text-[10.5px] tabular-nums text-slate-400">
              {draft.evidence.length} {draft.evidence.length === 1 ? 'finding' : 'findings'}
            </span>
          </div>
          <ul className="space-y-2.5">
            {draft.evidence.map((item, index) => {
              const muted = item.source === 'unavailable';
              return (
                <li key={`${item.claim}-${index}`} className="flex gap-2.5">
                  <span
                    className={`mt-[2px] w-5 shrink-0 font-mono text-[10px] tabular-nums ${muted ? 'text-amber-600' : 'text-slate-400'}`}
                  >
                    {pad2(index + 1)}
                  </span>
                  <button
                    onClick={() => {
                      if (typeof item.step_index === 'number') onSelectStep(item.step_index);
                      if (typeof item.trace_event_seq === 'number') onOpenTraceEvent(item.trace_event_seq);
                    }}
                    className="block w-full min-w-0 break-words text-left text-[12.5px] leading-5 text-slate-700 hover:text-slate-950"
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
        <div className="border-t border-[color:var(--color-hairline)] pt-3">
          <Eyebrow>Suggested failure label</Eyebrow>
          <span className="mt-1.5 inline-block rounded bg-red-50 px-2 py-0.5 font-mono text-[11px] font-semibold text-red-700">
            {draft.failure_type}
          </span>
        </div>
      )}

      {visualEvidence.length > 0 && (
        <div className="border-t border-[color:var(--color-hairline)] pt-3">
          <Eyebrow>Visual evidence</Eyebrow>
          <div className="mt-2 flex flex-wrap gap-2">
            {visualEvidence.map((item, index) => {
              const step = typeof item.step_index === 'number'
                ? run?.steps.find((c) => c.index === item.step_index)
                : null;
              const screenshot = step?.observation.screenshot;
              if (!run || !step || !screenshot) return null;
              return (
                <button
                  key={`${item.claim}-${index}`}
                  onClick={() => onSelectStep(step.index)}
                  className="h-12 w-20 overflow-hidden rounded-md border border-[color:var(--color-hairline)] bg-slate-100 hover:border-slate-400"
                  title={item.claim}
                >
                  <img
                    src={`/api/runs/${run.run_id}/screenshots/${screenshot}`}
                    alt={`Evidence step ${step.index}`}
                    className="h-full w-full object-cover"
                  />
                </button>
              );
            })}
          </div>
        </div>
      )}

      {draft.retrieved_context_ids.length > 0 && (
        <div className="border-t border-[color:var(--color-hairline)] pt-3">
          <Eyebrow>Retrieved context</Eyebrow>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {draft.retrieved_context_ids.map((id) => (
              <span
                key={id}
                title={id}
                className="max-w-full break-all rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10.5px] text-slate-700"
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

// Tiny shared atom — small uppercase mono kicker label. Inline so this
// patch stays a single function replacement.
function Eyebrow({ children }: { children: React.ReactNode }) {
  return (
    <span className="text-[10px] font-semibold uppercase tracking-[0.12em] text-slate-500">
      {children}
    </span>
  );
}

function pad2(n: number) {
  return n < 10 ? `0${n}` : String(n);
}
