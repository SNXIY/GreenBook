export type AssistantConversation = {
  conversation_id: string;
  title: string;
  context_post_id?: string | null;
  surface: "HOME" | "COMMENT" | "POST";
  updated_at: string;
};

export type AssistantMessage = {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  parts: AssistantToolPart[];
  run_id?: string | null;
  created_at: string;
};

export type AssistantToolPart = {
  tool: string;
  label: string;
  result: Record<string, unknown>;
};

export type AssistantRunAccepted = {
  run_id: string;
  conversation_id: string;
  status: string;
  events_url: string;
  replayed: boolean;
};

export type AssistantRunStep = {
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

export type AssistantRun = {
  run_id: string;
  conversation_id: string;
  goal: string;
  status: "QUEUED" | "RUNNING" | "RETRYING" | "WAITING_DEPENDENCY" | "WAITING_APPROVAL" | "PAUSED" | "COMPLETED" | "FAILED" | "CANCELLED";
  intent?: string | null;
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
  intent_detail?: Record<string, unknown> | null;
  task_ledger: Record<string, unknown>;
  progress_ledger: Record<string, unknown>;
  approval?: {
    approval_id: string;
    action: string;
    status: string;
    description: string;
    preview: Record<string, unknown>;
    expires_at: string;
    expected_run_version: number;
  } | null;
  steps: AssistantRunStep[];
  created_at: string;
  updated_at: string;
};

export type AssistantRunListItem = {
  run_id: string;
  conversation_id: string;
  goal: string;
  status: AssistantRun["status"];
  intent?: string | null;
  summary?: string | null;
  error?: string | null;
  trace_id: string;
  approval?: AssistantRun["approval"];
  steps: Array<Pick<AssistantRunStep, "step_id" | "label" | "status">>;
  creator_task_ids: string[];
  created_at: string;
  updated_at: string;
};

export type AssistantScheduledAction = {
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

export type AssistantMemory = {
  memory_id: string;
  key: string;
  value: string;
  created_at: string;
  updated_at: string;
};

export type AssistantMemoryProfile = {
  episodic_enabled: boolean;
  semantic_enabled: boolean;
  retention_days: number;
  semantic_backend: string;
  embedding_provider: string;
};

export type AssistantEpisode = {
  episode_id: string;
  run_id: string;
  intent?: string | null;
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
