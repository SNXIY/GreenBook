export type AgentConversation = {
  conversation_id: string;
  title: string;
  context_post_id?: string | null;
  surface: "HOME" | "COMMENT" | "POST";
  updated_at: string;
};

export type AgentMessage = {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  parts: AgentMessagePart[];
  run_id?: string | null;
  execution_id?: string | null;
  created_at: string;
};

export type AgentToolPart = {
  type?: "tool";
  tool: string;
  label: string;
  result: Record<string, unknown>;
};

export type AgentResultArtifact = {
  type: string;
  artifact_id: string;
  title?: string | null;
  content?: string | null;
  summary?: string | null;
  resource_type?: "DRAFT" | "SCHEDULE" | "POST" | string | null;
  resource_id?: string | null;
  run_at?: string | null;
  publish_time?: string | null;
  timezone?: string | null;
  status?: string | null;
  payload?: Record<string, unknown>;
};

export type AgentExecutionResultPart = {
  type: "execution_result";
  execution: {
    execution_id: string;
    task_id?: string;
    status: string;
    summary?: string;
    steps?: Array<{
      step_id?: string;
      label?: string;
      status?: string;
      error?: string | null;
    }>;
  };
  artifacts: AgentResultArtifact[];
  schedule?: Record<string, unknown> | null;
  next_actions: string[];
};

export type AgentClarificationCandidate = {
  identity: string;
  type: "TASK" | "DRAFT" | "SCHEDULE" | "POST" | "EXECUTION" | "APPROVAL";
  task_id?: string | null;
  resource_id?: string | null;
  artifact_id?: string | null;
  execution_id?: string | null;
  label?: string | null;
  status?: string | null;
};

export type AgentTargetClarificationPart = {
  type: "target_clarification";
  command: Record<string, unknown> & {
    target?: Record<string, unknown>;
  };
  candidates: AgentClarificationCandidate[];
};

export type AgentPolicyDecisionPart = {
  type: "policy_decision";
  policy_decision: Record<string, unknown>;
  audit_event: Record<string, unknown>;
};

export type AgentMessagePart =
  | AgentToolPart
  | AgentExecutionResultPart
  | AgentTargetClarificationPart
  | AgentPolicyDecisionPart;

export type AgentRunAccepted = {
  run_id: string;
  conversation_id: string;
  status: string;
  events_url: string;
  execution_id?: string | null;
  execution_ids?: string[];
  task_ids?: string[];
  execution_events_url?: string | null;
  error_code?: string | null;
  error?: string | null;
  replayed: boolean;
};

export type AgentRunStep = {
  step_id: string;
  ordinal: number;
  kind: string;
  tool_name?: string | null;
  label: string;
  status: "PENDING" | "RUNNING" | "WAITING_DEPENDENCY" | "WAITING_APPROVAL" | "COMPLETED" | "FAILED" | "CANCELLED";
  output?: Record<string, unknown> | null;
  error?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  task_key?: string | null;
  agent_name?: string | null;
  capabilities: string[];
  depends_on: string[];
  attempts: number;
  max_attempts: number;
};

export type AgentRun = {
  run_id: string;
  execution_id?: string | null;
  conversation_id: string;
  goal: string;
  status: "QUEUED" | "RUNNING" | "RETRYING" | "WAITING_DEPENDENCY" | "WAITING_LANE" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
  execution_path: "ROUTING" | "DIRECT" | "TOOL" | "CREATOR" | "ORCHESTRATED";
  workload_lane: "ROUTING" | "READ" | "WRITE";
  summary?: string | null;
  final_response?: string | null;
  error?: string | null;
  trace_id: string;
  budget: {
    model_calls: number;
    max_model_calls: number;
    tool_calls: number;
    max_tool_calls: number;
    replan_count: number;
    max_replans: number;
  };
  timing: {
    queue_ms: number | null;
    model_ms: number;
    tool_ms: number;
    dependency_wait_ms: number;
    total_ms: number | null;
  };
  task_ledger: Record<string, unknown>;
  progress_ledger: Record<string, unknown>;
  artifacts: AgentResultArtifact[];
  partial_results: Record<string, unknown>;
  approval?: {
    approval_id: string;
    action: string;
    status: string;
    description: string;
    preview: Record<string, unknown>;
    expires_at: string;
    expected_run_version: number;
  } | null;
  steps: AgentRunStep[];
  created_at: string;
  updated_at: string;
};

export type AgentRunListItem = {
  run_id: string;
  execution_id?: string | null;
  conversation_id: string;
  goal: string;
  status: AgentRun["status"];
  summary?: string | null;
  error?: string | null;
  trace_id: string;
  approval?: AgentRun["approval"];
  steps: Array<Pick<AgentRunStep, "step_id" | "label" | "status">>;
  creator_task_ids: string[];
  created_at: string;
  updated_at: string;
};

export type AgentScheduledAction = {
  action_id: string;
  run_id: string;
  draft_id: string;
  instruction: string;
  run_at: string;
  status: string;
  attempts: number;
  result?: Record<string, unknown> | null;
  error?: string | null;
};

export type AgentMemory = {
  memory_id: string;
  key: string;
  value: string;
  created_at: string;
  updated_at: string;
};

export type AgentMemoryProfile = {
  episodic_enabled: boolean;
  semantic_enabled: boolean;
  retention_days: number;
  semantic_backend: string;
  embedding_provider: string;
};

export type AgentEpisode = {
  episode_id: string;
  run_id: string;
  goal: string;
  summary: string;
  outcome: string;
  tool_names: string[];
  artifact_refs: Array<{ type: string; id: string }>;
  importance: number;
  occurred_at: string;
  expires_at: string;
  recall_count: number;
};
