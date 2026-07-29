import { apiFetch } from "./apiClient";
import type { NotificationPageResponse, UnreadCountResponse } from "@/types/notification";

export const notificationService = {
  list(cursor?: string | null, size = 20) {
    const params = new URLSearchParams({ size: String(size) });
    if (cursor) params.set("cursor", cursor);
    return apiFetch<NotificationPageResponse>(`/api/v1/notifications?${params.toString()}`);
  },

  unreadCount() {
    return apiFetch<UnreadCountResponse>("/api/v1/notifications/unread-count");
  },

  markRead(ids: string[]) {
    return apiFetch<void>("/api/v1/notifications/read", {
      method: "POST",
      body: { ids }
    });
  },

  markAllRead() {
    return apiFetch<void>("/api/v1/notifications/read-all", {
      method: "PATCH"
    });
  }
};
