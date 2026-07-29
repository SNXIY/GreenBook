import { apiFetch } from "./apiClient";
import type {
  ModerationAction,
  ModerationCallbackDelivery,
  ModerationStatistics,
  ModerationTask,
  ModerationTaskStatus,
  RiskType
} from "@/types/moderation";

const PREFIX = "/api/v1/admin/moderation";

export const moderationService = {
  statistics: () => apiFetch<ModerationStatistics>(`${PREFIX}/statistics`),

  callbacks: () =>
    apiFetch<ModerationCallbackDelivery[]>(`${PREFIX}/callbacks?limit=50`),

  tasks: (status?: ModerationTaskStatus) => {
    const query = status ? `?status=${encodeURIComponent(status)}` : "";
    return apiFetch<ModerationTask[]>(`${PREFIX}/tasks${query}`);
  },

  task: (taskId: string) =>
    apiFetch<ModerationTask>(`${PREFIX}/tasks/${encodeURIComponent(taskId)}`),

  review: (
    taskId: string,
    payload: {
      action: Exclude<ModerationAction, "HUMAN_REVIEW">;
      riskType?: RiskType;
      comment?: string;
      expectedVersion: number;
    }
  ) =>
    apiFetch<{ task: ModerationTask; case_created: boolean }>(
      `${PREFIX}/tasks/${encodeURIComponent(taskId)}/review`,
      { method: "POST", body: payload }
    )
};
