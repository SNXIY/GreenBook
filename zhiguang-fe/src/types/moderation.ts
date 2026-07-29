export type ModerationTaskStatus =
  | "PENDING"
  | "RUNNING"
  | "WAITING_REVIEW"
  | "COMPLETED"
  | "FAILED";

export type ModerationAction = "PASS" | "REJECT" | "LIMIT" | "HUMAN_REVIEW";
export type RiskType = "NORMAL" | "ADVERTISING" | "ABUSE" | "PRIVACY";

export type AgentDecision = {
  risk_type: RiskType;
  risk_score: number;
  confidence: number;
  recommended_action: ModerationAction;
  reason: string;
  indicators?: string[];
};

export type ModerationTask = {
  id: string;
  thread_id: string;
  trace_id?: string | null;
  status: ModerationTaskStatus;
  content: string;
  content_type: string;
  content_id?: string | null;
  platform?: string;
  creator_id?: string | null;
  metadata?: Record<string, unknown>;
  agent_decision?: AgentDecision | null;
  final_action?: ModerationAction | null;
  final_risk_type?: RiskType | null;
  human_decision?: {
    action: ModerationAction;
    risk_type?: RiskType | null;
    reviewer_id: string;
    comment?: string | null;
  } | null;
  error_message?: string | null;
  version: number;
  created_at: string;
  updated_at: string;
};

export type ModerationCallbackDelivery = {
  id: string;
  task_id: string;
  task_version: number;
  status: "PENDING" | "DELIVERING" | "RETRYING" | "DELIVERED" | "DEAD";
  attempts: number;
  max_attempts: number;
  available_at: string;
  last_http_status?: number | null;
  last_error?: string | null;
  updated_at: string;
  delivered_at?: string | null;
};

export type ModerationStatistics = {
  total_tasks: number;
  pending_review: number;
  agent_human_disagreements: number;
  by_status: Partial<Record<ModerationTaskStatus, number>>;
  by_risk_type: Partial<Record<RiskType, number>>;
  by_action: Partial<Record<ModerationAction, number>>;
};
