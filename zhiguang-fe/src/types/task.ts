export type CreatorTaskStatus =
  | "CREATED"
  | "QUEUED"
  | "RUNNING"
  | "WAITING_HUMAN"
  | "RETRYING"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export type CreatorTaskListItem = {
  task_id: string;
  run_id: string;
  kind: "CREATE_CONTENT" | "ANALYZE_CONTENT" | "BUILD_STRATEGY" | "IMPROVE_DRAFT" | "RESEARCH_TOPIC";
  goal: string;
  status: CreatorTaskStatus;
  version: number;
  pending_decision_id?: string | null;
  final_artifact_id?: string | null;
  error_code?: string | null;
  created_at: string;
  updated_at: string;
};

export type CreatorTaskPage = {
  items: CreatorTaskListItem[];
  next_cursor?: string | null;
};

export type PostTaskItem = {
  id: string;
  title?: string | null;
  status: "draft" | "reviewing" | "published" | "rejected";
  contentOrigin?: "MANUAL" | "AI_ASSISTED" | null;
  moderationTaskId?: string | null;
  reason?: string | null;
  createdAt: string;
  updatedAt: string;
  publishedAt?: string | null;
};
