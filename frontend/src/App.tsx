import { useEffect, useMemo, useState } from 'react';
import './App.css';
import { EvalAgentPanel } from './components/EvalAgentPanel';
import { RunList } from './components/RunList';
import { ScreenshotViewer } from './components/ScreenshotViewer';
import { StepDetailPanel } from './components/StepDetailPanel';
import { StepTimeline } from './components/StepTimeline';
import type { EvalResult, TrajectoryRun } from './types';

const API_BASE = import.meta.env.VITE_API_BASE ?? 'http://127.0.0.1:8000';

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Request failed: ${response.status}`);
  return response.json() as Promise<T>;
}

function App() {
  const [runs, setRuns] = useState<TrajectoryRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const [result, setResult] = useState<EvalResult | null>(null);

  useEffect(() => {
    const load = async () => {
      const runList = await fetchJson<{ runs: string[] }>(`${API_BASE}/runs`);
      const loaded = await Promise.all(
        runList.runs.map((runId) => fetchJson<TrajectoryRun>(`${API_BASE}/runs/${runId}`))
      );
      setRuns(loaded);
      if (loaded.length > 0) {
        setSelectedRunId(loaded[0].run_id);
        setSelectedStepId(loaded[0].steps[0]?.step_id ?? null);
      }
    };

    load().catch(() => {
      setRuns([]);
    });
  }, []);

  const selectedRun = useMemo(
    () => runs.find((run) => run.run_id === selectedRunId) ?? null,
    [runs, selectedRunId]
  );

  const selectedStep = useMemo(
    () => selectedRun?.steps.find((step) => step.step_id === selectedStepId) ?? null,
    [selectedRun, selectedStepId]
  );

  const analyze = async (stepId: string | null) => {
    if (!selectedRunId || !stepId) return;
    const next = await fetchJson<EvalResult>(`${API_BASE}/analyze/${selectedRunId}/${stepId}`);
    setResult(next);
  };

  return (
    <main className="layout">
      <h1>EvalTrace Lite</h1>
      <p>Trajectory-to-eval-case agent for human-reviewed regression cases.</p>
      <section className="grid">
        <RunList
          runs={runs}
          selectedRunId={selectedRunId}
          onSelect={(runId) => {
            setSelectedRunId(runId);
            const nextRun = runs.find((run) => run.run_id === runId);
            setSelectedStepId(nextRun?.steps[0]?.step_id ?? null);
            setResult(null);
          }}
        />
        <StepTimeline
          steps={selectedRun?.steps ?? []}
          selectedStepId={selectedStepId}
          onSelectStep={(stepId) => {
            setSelectedStepId(stepId);
            setResult(null);
          }}
        />
        <ScreenshotViewer runId={selectedRunId ?? 'n/a'} step={selectedStep} />
        <StepDetailPanel step={selectedStep} />
      </section>
      <EvalAgentPanel
        result={result}
        disabled={!selectedRunId || !selectedStepId}
        onAnalyzeRun={() => analyze(selectedRun?.steps.find((s) => !s.success)?.step_id ?? selectedStepId)}
        onAnalyzeStep={() => analyze(selectedStepId)}
      />
    </main>
  );
}

export default App;
