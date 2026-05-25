// Pydantic schemas ported to TypeScript interfaces

export interface Coordinate {
  x: number;
  y: number;
}

export interface BBox {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface StepAction {
  type: "click" | "type" | "scroll" | "navigate" | "wait" | "unknown";
  label?: string;
  text?: string;
  coordinates?: Coordinate;
  bbox?: BBox;
  raw?: string;
}

export interface StepObservation {
  screenshot?: string;
  url?: string;
  title?: string;
  visible_text?: string;
  visual_evidence: string[];
}

export interface StepResult {
  status: "success" | "failed" | "unknown";
  error?: string;
}

export interface CoordinateValidation {
  status: "validated" | "out_of_bounds" | "missing" | "unknown";
  image_width?: number;
  image_height?: number;
  reason?: string;
}

export interface TrajectoryStep {
  index: number;
  timestamp?: string;
  observation: StepObservation;
  action: StepAction;
  result: StepResult;
  coordinate_validation: CoordinateValidation;
  metadata: Record<string, unknown>;
}

export interface TrajectoryRun {
  run_id: string;
  task: string;
  source: string;
  status: "success" | "failed" | "unknown";
  steps: TrajectoryStep[];
  metadata: Record<string, unknown>;
}

export interface StepDigest {
  index: number;
  action_type: "click" | "type" | "scroll" | "navigate" | "wait" | "unknown";
  action_text: string;
  action_target?: string;
  url?: string;
  title?: string;
  result_status: "success" | "failed" | "unknown";
  coord_validation_status: "validated" | "out_of_bounds" | "missing" | "unknown";
  vlm_low_detail_summary?: string;
  has_screenshot: boolean;
}

export interface TrajectoryDigest {
  run_id: string;
  task: string;
  step_count: number;
  steps: StepDigest[];
  preprocess_model?: string;
  preprocess_version: string;
}

export interface FailureMemoryCase {
  case_id: string;
  failure_type: string;
  summary: string;
  fix_hint?: string;
  tags: string[];
  source_run_id?: string;
}

export interface EvidenceItem {
  claim: string;
  source: "trajectory" | "trajectory_digest" | "step_detail_high" | "step_detail_low" | "failure_memory" | "eval_case" | "successful_run" | "unavailable";
  run_id?: string;
  step_index?: number;
  trace_event_seq?: number;
  context_id?: string;
}

export interface EvalCase {
  case_id: string;
  source_run_id: string;
  task: string;
  // Failure-shape fields. Either all five are present (failure case) or all
  // five are null (success case). The backend model_validator enforces the
  // XOR; the frontend tolerates both shapes when rendering.
  failure_step: number | null;
  failure_type: string | null;
  expected_behavior: string | null;
  actual_behavior: string | null;
  evidence: EvidenceItem[];
  regression_rule: string | null;
  retrieved_context_ids: string[];
  human_validated: boolean;
}

export interface AgentTraceEvent {
  seq: number;
  type: "agent_message" | "user_message" | "tool_call" | "tool_result" | "tool_error" | "phase";
  name?: string;
  args?: Record<string, unknown>;
  result?: Record<string, unknown>;
  message?: string;
  error?: string;
  turn: number;
}

export interface AgentTrace {
  run_id: string;
  user_intent: "analyze_run" | "analyze_step";
  selected_step?: number;
  tool_call_count: number;
  turn_count: number;
  terminated_by: "propose_eval_case" | "budget_exceeded" | "error";
  events: AgentTraceEvent[];
}
