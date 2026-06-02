import { useEffect, useMemo, useState } from 'react';
import { Header } from './components/Header';
import { TrajectoryList } from './components/TrajectoryList';
import { StepTimeline } from './components/StepTimeline';
import { ScreenshotViewer } from './components/ScreenshotViewer';
import { StepDetailPanel } from './components/StepDetailPanel';
import { EvalAgentPanel } from './components/EvalAgentPanel';
import { useUrlState } from './hooks/useUrlState';
import { fetchTrajectories, fetchTrajectory } from './api/client';
import type { AgentTrace, EvalCase, TrajectoryDigest, Trajectory } from './types/contracts';

function App() {
  const { trajectoryId, stepIndex, setTrajectoryId, setStepIndex } = useUrlState();
  const [trajectories, setTrajectories] = useState<Trajectory[]>([]);
  const [selectedTrajectory, setSelectedTrajectory] = useState<Trajectory | null>(null);
  const [agentTrace, setAgentTrace] = useState<AgentTrace | null>(null);
  const [evalCaseDraft, setEvalCaseDraft] = useState<EvalCase | null>(null);
  // Cached digest comes back with the trajectory; the right-panel cost helper
  // uses its VLM preprocess token counts to build the total.
  const [trajectoryDigest, setTrajectoryDigest] = useState<TrajectoryDigest | null>(null);
  const [loadingTrajectories, setLoadingTrajectories] = useState(true);
  const [loadingTrajectoryDetails, setLoadingTrajectoryDetails] = useState(false);
  const [trajectoryError, setTrajectoryError] = useState<string | null>(null);
  const [stepDetailsExpanded, setStepDetailsExpanded] = useState(false);

  const loadTrajectories = async () => {
    setLoadingTrajectories(true);
    try {
      const data = await fetchTrajectories();
      setTrajectories(data);
      setTrajectoryError(null);
    } catch (e) {
      console.error(e);
      setTrajectoryError('Failed to load trajectories');
    } finally {
      setLoadingTrajectories(false);
    }
  };

  useEffect(() => {
    loadTrajectories();
  }, []);

  useEffect(() => {
    if (trajectoryId) {
      const loadTrajectoryDetails = async () => {
        setLoadingTrajectoryDetails(true);
        try {
          const trajectory = await fetchTrajectory(trajectoryId);
          setSelectedTrajectory(trajectory);
          setTrajectoryDigest(trajectory.digest ?? null);
          setAgentTrace(trajectory.last_trace ?? null);
          setEvalCaseDraft(trajectory.eval_case_draft ?? deriveLatestDraft(trajectory.last_trace ?? null));
          setTrajectoryError(null);
        } catch (e) {
          console.error(e);
          setTrajectoryError(`Trajectory not found: ${trajectoryId}`);
          setSelectedTrajectory(null);
          setTrajectoryDigest(null);
          setAgentTrace(null);
          setEvalCaseDraft(null);
        } finally {
          setLoadingTrajectoryDetails(false);
        }
      };
      loadTrajectoryDetails();
    } else {
      setSelectedTrajectory(null);
      setTrajectoryDigest(null);
      setAgentTrace(null);
      setEvalCaseDraft(null);
      setTrajectoryError(null);
    }
  }, [trajectoryId]); // We don't want to re-fetch if only stepIndex changes

  useEffect(() => {
    if (selectedTrajectory && stepIndex === null && selectedTrajectory.steps.length > 0) {
      setStepIndex(selectedTrajectory.steps[0].index);
    }
  }, [selectedTrajectory, setStepIndex, stepIndex]);

  useEffect(() => {
    setStepDetailsExpanded(false);
  }, [trajectoryId]);

  const activeStepIndex = stepIndex !== null ? stepIndex : selectedTrajectory?.steps[0]?.index ?? 0;
  const activeStepPosition = selectedTrajectory ? selectedTrajectory.steps.findIndex((step) => step.index === activeStepIndex) : -1;
  const activeStep = selectedTrajectory
    ? selectedTrajectory.steps.find((step) => step.index === activeStepIndex) ?? selectedTrajectory.steps[activeStepIndex] ?? null
    : null;
  const inspectedSteps = useMemo(() => inspectedStepSet(agentTrace), [agentTrace]);

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden bg-[color:var(--color-canvas)] font-sans text-slate-900">
      <Header onReload={loadTrajectories} trajectories={trajectories} datasetLabel={datasetLabel(trajectories)} />
      
      <main className="flex min-h-0 flex-1 flex-col gap-3 overflow-auto p-3 xl:flex-row xl:overflow-hidden">
        <TrajectoryList 
          trajectories={trajectories} 
          selectedTrajectoryId={trajectoryId} 
          onSelectTrajectory={(id) => setTrajectoryId(id)} 
        />
        
        <section className="flex min-h-[520px] min-w-0 flex-1 flex-col overflow-hidden rounded-lg border border-slate-200 bg-white shadow-sm xl:min-h-0">
          {loadingTrajectoryDetails ? (
            <div className="flex flex-1 items-center justify-center text-sm text-slate-500">Loading trajectory details...</div>
          ) : trajectoryError && trajectoryId ? (
            <div className="flex flex-1 flex-col items-center justify-center px-6 text-center text-red-600">
              <svg className="mb-4 h-14 w-14 text-red-200" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.4" d="M12 8v4m0 4h.01M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" /></svg>
              <p className="font-semibold">{trajectoryError}</p>
              <p className="mt-2 text-sm text-slate-500">Make sure the backend is running at http://localhost:8000 and the dataset has been imported.</p>
            </div>
          ) : selectedTrajectory && activeStep ? (
            <>
              <div className="bg-white">
                <div className="border-b border-[color:var(--color-hairline)] px-4 py-3">
                  <div className="min-w-0">
                    <div className="text-[10.5px] font-semibold uppercase tracking-[0.12em] text-slate-500">Task</div>
                    <p className="mt-1 break-words text-[13px] leading-5 text-slate-800">
                      <span className="text-slate-400">navigate:</span> {selectedTrajectory.task}
                    </p>
                  </div>
                </div>
                <StepTimeline
                  trajectory={selectedTrajectory} 
                  selectedStepIndex={activeStepIndex} 
                  inspectedSteps={inspectedSteps}
                  onSelectStep={setStepIndex} 
                />
              </div>
              
              <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-slate-50/70">
                <ScreenshotViewer 
                  trajectoryId={selectedTrajectory.trajectory_id} 
                  step={activeStep} 
                  totalSteps={selectedTrajectory.steps.length}
                  detailsExpanded={stepDetailsExpanded}
                  onPrev={() => setStepIndex(selectedTrajectory.steps[Math.max(0, activeStepPosition - 1)]?.index ?? activeStepIndex)}
                  onNext={() => setStepIndex(selectedTrajectory.steps[Math.min(selectedTrajectory.steps.length - 1, activeStepPosition + 1)]?.index ?? activeStepIndex)}
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
              <p>{loadingTrajectories ? 'Loading trajectories...' : 'Select a trajectory from the left panel.'}</p>
            </div>
          )}
        </section>

        <EvalAgentPanel
          trajectory={selectedTrajectory}
          digest={trajectoryDigest}
          selectedStepIndex={activeStep?.index ?? null}
          trace={agentTrace}
          evalCaseDraft={evalCaseDraft}
          onTraceChange={setAgentTrace}
          onDraftChange={setEvalCaseDraft}
          onSelectStep={setStepIndex}
          onEvalCaseValidated={loadTrajectories}
        />
      </main>
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

function datasetLabel(trajectories: Trajectory[]): string {
  // Prefer the dataset 'source' on the first trajectory (matches what the old
  // Footer showed). Fall back to the bundled sample's label.
  return trajectories[0]?.source ?? 'allenai / MolmoWeb-HumanSkills';
}
