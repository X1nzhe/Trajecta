export type Coordinates = { x: number; y: number };

export type TrajectoryStep = {
  step_id: string;
  action: string;
  target?: string | null;
  screenshot_path: string;
  timestamp?: string | null;
  success: boolean;
  error?: string | null;
  coordinates?: Coordinates | null;
};

export type TrajectoryRun = {
  run_id: string;
  source: string;
  steps: TrajectoryStep[];
};

export type EvalResult = {
  analysis: {
    failure_label: string;
    confidence: number;
    reasoning: string;
  };
  eval_case: {
    eval_case_id: string;
    run_id: string;
    step_id: string;
    failure_label: string;
    status: string;
    summary: string;
    evidence: string[];
    similar_case_ids: string[];
  };
  retrieved_cases: Array<{ case_id: string; failure_label: string; summary: string }>;
};
