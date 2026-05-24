import type { AgentTrace, AgentTraceEvent, EvalCase } from '../types/contracts';

export interface AgentStreamDone {
  type: 'done';
  eval_case_draft: EvalCase | null;
  agent_trace: AgentTrace;
}

type AgentStreamLine =
  | { type: 'event'; event: AgentTraceEvent }
  | AgentStreamDone
  | { type: 'error'; error: string };

interface StreamAgentOptions {
  body?: unknown;
  signal?: AbortSignal;
  onEvent: (event: AgentTraceEvent) => void;
}

export async function streamAgentRequest(
  url: string,
  { body, signal, onEvent }: StreamAgentOptions,
): Promise<AgentStreamDone> {
  const res = await fetch(url, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    throw new Error(await responseErrorMessage(res));
  }

  if (!res.body) {
    throw new Error('Agent response did not include a stream body.');
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop() ?? '';
    const terminal = parseLines(lines, onEvent);
    if (terminal) return terminal;
  }

  buffer += decoder.decode();
  const terminal = parseLines(buffer.split('\n'), onEvent);
  if (terminal) return terminal;
  throw new Error('Agent stream ended without a terminal result.');
}

function parseLines(
  lines: string[],
  onEvent: (event: AgentTraceEvent) => void,
): AgentStreamDone | null {
  for (const line of lines) {
    if (!line.trim()) continue;
    const message = JSON.parse(line) as AgentStreamLine;
    if (message.type === 'event') {
      onEvent(message.event);
    } else if (message.type === 'done') {
      return message;
    } else if (message.type === 'error') {
      throw new Error(message.error);
    }
  }
  return null;
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
