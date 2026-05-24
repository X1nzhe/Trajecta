import type { AgentTrace, EvalCase, TrajectoryDigest, TrajectoryRun } from '../types/contracts';

export interface RunDetail extends TrajectoryRun {
  digest?: TrajectoryDigest;
  last_trace?: AgentTrace | null;
  eval_case_draft?: EvalCase | null;
}

export async function fetchRuns(): Promise<TrajectoryRun[]> {
  const res = await fetch('/api/runs');
  if (!res.ok) throw new Error('Failed to fetch runs');
  return res.json();
}

export async function fetchRun(runId: string): Promise<RunDetail> {
  const res = await fetch(`/api/runs/${runId}`);
  if (!res.ok) throw new Error('Failed to fetch run details');
  return res.json();
}

export async function fetchRunDigest(runId: string): Promise<TrajectoryDigest> {
  const res = await fetch(`/api/runs/${runId}/digest`);
  if (!res.ok) throw new Error('Failed to fetch run digest');
  return res.json();
}

export async function importDataset(): Promise<void> {
  const res = await fetch('/api/import/molmoweb-sample', { method: 'POST' });
  if (!res.ok) throw new Error('Failed to import dataset');
}

export async function createEvalCase(caseDraft: EvalCase): Promise<EvalCase> {
  const res = await fetch('/api/eval-cases', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(caseDraft),
  });
  if (!res.ok) throw new Error(await responseErrorMessage(res));
  return res.json();
}

async function responseErrorMessage(res: Response): Promise<string> {
  const text = await res.text();
  if (!text) return `Request failed with ${res.status}`;
  try {
    const payload = JSON.parse(text) as { detail?: unknown };
    if (typeof payload.detail === 'string') return payload.detail;
    if (payload.detail !== undefined) return JSON.stringify(payload.detail);
  } catch {
    return text;
  }
  return text;
}
