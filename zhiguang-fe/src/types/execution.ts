export type ExecutionStatus =
  | "PENDING"
  | "RUNNING"
  | "PAUSED"
  | "WAITING_APPROVAL"
  | "WAITING_HUMAN"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export type ExecutionControlState =
  | "RUNNING"
  | "PAUSING"
  | "PAUSED"
  | "RESUMING"
  | "CANCELLED";

export type ExecutionStepStatus =
  | "PENDING"
  | "RUNNING"
  | "WAITING_APPROVAL"
  | "COMPLETED"
  | "FAILED_RETRYABLE"
  | "FAILED"
  | "SKIPPED";

export type ExecutionStep = {
  step_execution_id: string;
  step_id: string;
  capability: string;
  status: ExecutionStepStatus | string;
  retry_count: number;
  error_code: string;
  error_message: string;
  started_at: string;
  completed_at: string;
};

export type ExecutionEvent = {
  event_id: string;
  execution_id: string;
  event_type: string;
  step_id?: string | null;
  timestamp: string;
  payload: Record<string, unknown>;
};

/** Runtime execution projection.  Run fields remain optional compatibility metadata. */
export type Execution = {
  execution_id: string;
  task_id?: string;
  plan_id?: string;
  status: ExecutionStatus | string;
  current_step: string;
  progress: number;
  total_steps: number;
  completed_steps: number;
  created_at: string;
  updated_at: string;
  control_state: ExecutionControlState | string;
  control_reason?: string;
  control_requested_at?: string;
  steps?: ExecutionStep[];
  events?: ExecutionEvent[];
  run_id?: string | null;
  error_code?: string | null;
  error_message?: string | null;
};

export type ExecutionControl = {
  execution_id: string;
  state: ExecutionControlState | string;
  reason: string;
  requested_at: string;
  updated_at: string;
  execution_status: ExecutionStatus | string;
  current_step: string;
};

export type ExecutionListResponse = {
  items: Execution[];
  next_cursor?: string | null;
};

export type ExecutionStepsResponse = {
  execution_id: string;
  steps: ExecutionStep[];
};

export type ExecutionEventsResponse = {
  execution_id: string;
  events: ExecutionEvent[];
};
