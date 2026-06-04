import type { AgentTrace, EvalCase, TrajectoryDigest, Trajectory } from '../types/contracts';

export interface TrajectoryDetail extends Trajectory {
  digest?: TrajectoryDigest;
  last_trace?: AgentTrace | null;
  eval_case_draft?: EvalCase | null;
}

export async function fetchTrajectories(): Promise<Trajectory[]> {
  const res = await fetch('/api/trajectories');
  if (!res.ok) throw new Error('Failed to fetch trajectories');
  return res.json();
}

export async function fetchTrajectory(trajectoryId: string): Promise<TrajectoryDetail> {
  const res = await fetch(`/api/trajectories/${trajectoryId}`);
  if (!res.ok) throw new Error('Failed to fetch trajectory details');
  return res.json();
}

export async function fetchTrajectoryDigest(trajectoryId: string): Promise<TrajectoryDigest> {
  const res = await fetch(`/api/trajectories/${trajectoryId}/digest`);
  if (!res.ok) throw new Error('Failed to fetch trajectory digest');
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
