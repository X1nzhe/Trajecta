import type { EvalResult } from '../types';

type Props = {
  result: EvalResult | null;
  onAnalyzeRun: () => void;
  onAnalyzeStep: () => void;
  disabled: boolean;
};

export function EvalAgentPanel({ result, onAnalyzeRun, onAnalyzeStep, disabled }: Props) {
  return (
    <div>
      <h3>Eval Agent</h3>
      <div style={{ display: 'flex', gap: 8 }}>
        <button type="button" onClick={onAnalyzeRun} disabled={disabled}>Analyze Run</button>
        <button type="button" onClick={onAnalyzeStep} disabled={disabled}>Analyze Step</button>
      </div>
      {result && (
        <div style={{ marginTop: 12 }}>
          <p><strong>Failure Label:</strong> {result.analysis.failure_label}</p>
          <p><strong>Confidence:</strong> {result.analysis.confidence.toFixed(2)}</p>
          <p><strong>Reasoning:</strong> {result.analysis.reasoning}</p>
          <p><strong>Eval Case:</strong> {result.eval_case.eval_case_id}</p>
        </div>
      )}
    </div>
  );
}
