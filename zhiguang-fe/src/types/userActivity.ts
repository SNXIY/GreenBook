/**
 * Public business-progress contract from the Agent API.
 *
 * This intentionally has no execution id, tool name, MCP detail, lease, or
 * raw error field.  It is the only live-progress payload rendered in the
 * normal community-facing Agent panel.
 */
export type UserActivityType =
  | "SEARCH_STARTED"
  | "SEARCH_COMPLETED"
  | "SUMMARIZATION_STARTED"
  | "SUMMARIZATION_COMPLETED"
  | "DRAFT_LOOKUP_STARTED"
  | "DRAFT_LOOKUP_COMPLETED"
  | "DRAFT_CREATING"
  | "DRAFT_CREATED"
  | "DRAFT_UPDATING"
  | "DRAFT_UPDATED"
  | "DRAFT_DELETING"
  | "DRAFT_DELETED"
  | "SCHEDULE_LOOKUP_STARTED"
  | "SCHEDULE_LOOKUP_COMPLETED"
  | "SCHEDULE_CREATING"
  | "SCHEDULE_CREATED"
  | "SCHEDULE_UPDATING"
  | "SCHEDULE_UPDATED"
  | "SCHEDULE_CANCELLING"
  | "SCHEDULE_CANCELLED"
  | "PUBLISHING"
  | "PUBLISHED"
  | "REPLYING"
  | "REPLIED"
  | "ANALYTICS_LOADING"
  | "ANALYTICS_COMPLETED"
  | "NEEDS_CLARIFICATION"
  | "NEEDS_SEMANTIC_CONFIRMATION"
  | "NEEDS_APPROVAL"
  | "RESULT_UNKNOWN"
  | "RECONCILING"
  | "FAILED";

export type UserActivityStatus =
  | "IN_PROGRESS"
  | "COMPLETED"
  | "FAILED"
  | "WAITING_CLARIFICATION"
  | "WAITING_SEMANTIC_CONFIRMATION"
  | "WAITING_APPROVAL"
  | "RESULT_UNKNOWN"
  | "RECONCILING";

export type UserActivityResourceRef = {
  ref: string;
  kind: string;
  resource_id: string;
  version?: number | null;
};

export type UserActivityEvent = {
  activity_id: string;
  conversation_id: string;
  run_id?: string | null;
  task_id?: string | null;
  objective_id?: string | null;
  resource_ref?: UserActivityResourceRef | null;
  activity_type: UserActivityType;
  status: UserActivityStatus;
  display_key: string;
  safe_payload: Record<string, unknown>;
  sequence: number;
  created_at: string;
  verified_at?: string | null;
  terminal: boolean;
};

export type UserActivityListResponse = {
  items: UserActivityEvent[];
  next_cursor: number;
};
