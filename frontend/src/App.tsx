import { useEffect, useMemo, useState } from 'react';
import { Header } from './components/Header';
import { Footer } from './components/Footer';
import { RunList } from './components/RunList';
import { StepTimeline } from './components/StepTimeline';
import { ScreenshotViewer } from './components/ScreenshotViewer';
import { StepDetailPanel } from './components/StepDetailPanel';
import { EvalAgentPanel } from './components/EvalAgentPanel';
import { useUrlState } from './hooks/useUrlState';
import { fetchRuns, fetchRun } from './api/client';
import type { AgentTrace, EvalCase, TrajectoryRun } from './types/contracts';

function App() {
  const { runId, stepIndex, setRunId, setStepIndex } = useUrlState();
  const [runs, setRuns] = useState<TrajectoryRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<TrajectoryRun | null>(null);
  const [agentTrace, setAgentTrace] = useState<AgentTrace | null>(null);
  const [evalCaseDraft, setEvalCaseDraft] = useState<EvalCase | null>(null);
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [loadingRunDetails, setLoadingRunDetails] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [stepDetailsExpanded, setStepDetailsExpanded] = useState(false);

  const loadRuns = async () => {
    setLoadingRuns(true);
    try {
      const data = await fetchRuns();
      setRuns(data);
      setRunError(null);
    } catch (e) {
      console.error(e);
      setRunError('Failed to load trajectories');
    } finally {
      setLoadingRuns(false);
    }
  };

  useEffect(() => {
    loadRuns();
  }, []);

  useEffect(() => {
    if (runId) {
      const loadRunDetails = async () => {
        setLoadingRunDetails(true);
        try {
          const run = await fetchRun(runId);
          setSelectedRun(run);
          setAgentTrace(run.last_trace ?? null);
          setEvalCaseDraft(run.eval_case_draft ?? deriveLatestDraft(run.last_trace ?? null));
          setRunError(null);
        } catch (e) {
          console.error(e);
          setRunError(`Trajectory not found: ${runId}`);
          setSelectedRun(null);
          setAgentTrace(null);
          setEvalCaseDraft(null);
        } finally {
          setLoadingRunDetails(false);
        }
      };
      loadRunDetails();
    } else {
      setSelectedRun(null);
      setAgentTrace(null);
      setEvalCaseDraft(null);
      setRunError(null);
    }
  }, [runId]); // We don't want to re-fetch if only stepIndex changes

  useEffect(() => {
    if (selectedRun && stepIndex === null && selectedRun.steps.length > 0) {
      setStepIndex(selectedRun.steps[0].index);
    }
  }, [selectedRun, setStepIndex, stepIndex]);

  useEffect(() => {
    setStepDetailsExpanded(false);
  }, [runId]);

  const activeStepIndex = stepIndex !== null ? stepIndex : selectedRun?.steps[0]?.index ?? 0;
  const activeStepPosition = selectedRun ? selectedRun.steps.findIndex((step) => step.index === activeStepIndex) : -1;
  const activeStep = selectedRun
    ? selectedRun.steps.find((step) => step.index === activeStepIndex) ?? selectedRun.steps[activeStepIndex] ?? null
    : null;
  const inspectedSteps = useMemo(() => inspectedStepSet(agentTrace), [agentTrace]);

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-[#f4f5f8] font-sans text-slate-900">
      <Header onReload={loadRuns} />
      
      <main className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-3 xl:flex-row xl:overflow-hidden">
        <RunList 
          runs={runs} 
          selectedRunId={runId} 
          onSelectRun={(id) => setRunId(id)} 
        />
        
        <section className="flex min-h-[520px] min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm xl:min-h-0">
          {loadingRunDetails ? (
            <div className="flex flex-1 items-center justify-center text-sm text-slate-500">Loading trajectory details...</div>
          ) : runError && runId ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 text-center text-red-600">
              <svg className="mb-4 h-14 w-14 text-red-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.4" d="M12 8v4m0 4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
              <p className="font-semibold">{runError}</p>
              <p className="mt-2 text-sm text-slate-500">Make sure the backend is running at http://localhost:8000 and the dataset has been imported.</p>
            </div>
          ) : selectedRun && activeStep ? (
            <>
              <div className="bg-white">
                <div className="border-b border-slate-200 px-4 py-2.5">
                  <div className="min-w-0">
                    <div className="mb-1 flex items-center gap-2">
                      <h2 className="truncate text-base font-bold text-slate-950" title={selectedRun.run_id}>Trajectory {truncateRunId(selectedRun.run_id)}</h2>
                      <span className={`rounded-full px-2 py-0.5 text-[11px] font-semibold ${runStatusClass(selectedRun.status)}`}>
                        {runStatusLabel(selectedRun.status)}
                      </span>
                    </div>
                    <div className="break-words text-xs leading-4 text-slate-600">
                      <span className="font-semibold text-slate-700">Task:</span> {selectedRun.task}
                    </div>
                  </div>
                </div>
                <StepTimeline 
                  run={selectedRun} 
                  selectedStepIndex={activeStepIndex} 
                  inspectedSteps={inspectedSteps}
                  onSelectStep={setStepIndex} 
                />
              </div>
              
              <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-slate-50/70">
                <ScreenshotViewer 
                  runId={selectedRun.run_id} 
                  step={activeStep} 
                  totalSteps={selectedRun.steps.length}
                  detailsExpanded={stepDetailsExpanded}
                  onPrev={() => setStepIndex(selectedRun.steps[Math.max(0, activeStepPosition - 1)]?.index ?? activeStepIndex)}
                  onNext={() => setStepIndex(selectedRun.steps[Math.min(selectedRun.steps.length - 1, activeStepPosition + 1)]?.index ?? activeStepIndex)}
                />
                <StepDetailPanel
                  step={activeStep}
                  isExpanded={stepDetailsExpanded}
                  onExpandedChange={setStepDetailsExpanded}
                />
              </div>
            </>
          ) : (
            <div className="flex flex-1 flex-col items-center justify-center px-6 text-center text-slate-500">
              <svg className="mb-4 h-14 w-14 text-slate-300" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.4" d="M4 7a2 2 0 0 1 2-2h5l2 2h5a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7Z" /></svg>
              <p>{loadingRuns ? 'Loading trajectories...' : 'Select a trajectory from the left panel.'}</p>
            </div>
          )}
        </section>

        <EvalAgentPanel
          run={selectedRun}
          selectedStepIndex={activeStep?.index ?? null}
          trace={agentTrace}
          evalCaseDraft={evalCaseDraft}
          onTraceChange={setAgentTrace}
          onDraftChange={setEvalCaseDraft}
          onSelectStep={setStepIndex}
          onEvalCaseValidated={loadRuns}
        />
      </main>

      <Footer runs={runs} />
    </div>
  );
}

export default App;

function inspectedStepSet(trace: AgentTrace | null) {
  const steps = new Set<number>();
  for (const event of trace?.events ?? []) {
    if (event.type === 'tool_call' && event.name === 'get_step_detail') {
      const value = event.args?.step_index;
      if (typeof value === 'number') steps.add(value);
    }
  }
  return steps;
}

function deriveLatestDraft(trace: AgentTrace | null): EvalCase | null {
  for (const event of [...(trace?.events ?? [])].reverse()) {
    if (event.type === 'tool_result' && event.name === 'propose_eval_case' && event.result) {
      return event.result as unknown as EvalCase;
    }
  }
  return null;
}

function runStatusClass(status: TrajectoryRun['status']) {
  if (status === 'failed') return 'bg-red-50 text-red-700';
  if (status === 'success') return 'bg-emerald-50 text-emerald-700';
  return 'bg-amber-50 text-amber-700';
}

function runStatusLabel(status: TrajectoryRun['status']) {
  // Title-cased to match RunList's StatusBadge labels.
  if (status === 'failed') return 'Failed';
  if (status === 'success') return 'Success';
  return 'Unverified';
}

function truncateRunId(id: string) {
  if (id.length <= 18) return id;
  return `${id.slice(0, 10)}...${id.slice(-6)}`;
}
