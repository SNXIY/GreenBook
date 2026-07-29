import { apiFetch } from "./apiClient";
import type { CommentCreateRequest, CommentItem, CommentPageResponse } from "@/types/comment";

const COMMENT_PREFIX = "/api/v1/comments";

export const commentService = {
  create: (payload: CommentCreateRequest, accessToken: string) =>
    apiFetch<CommentItem>(COMMENT_PREFIX, {
      method: "POST",
      body: payload,
      accessToken
    }),

  list: (postId: string, parentId?: string, cursor?: string | null, size = 20, accessToken?: string) => {
    const params = new URLSearchParams({ postId, size: String(size) });
    if (parentId) params.set("parentId", parentId);
    if (cursor) params.set("cursor", cursor);
    return apiFetch<CommentPageResponse>(`${COMMENT_PREFIX}?${params.toString()}`, {
      accessToken: accessToken ?? null
    });
  },

  hot: (postId: string, size = 5, accessToken?: string) =>
    apiFetch<CommentItem[]>(`${COMMENT_PREFIX}/hot?postId=${postId}&size=${size}`, {
      accessToken: accessToken ?? null
    }),

  remove: (commentId: string, accessToken: string) =>
    apiFetch<void>(`${COMMENT_PREFIX}/${commentId}`, {
      method: "DELETE",
      accessToken
    }),

  setTop: (commentId: string, top: boolean, accessToken: string) =>
    apiFetch<void>(`${COMMENT_PREFIX}/${commentId}/top`, {
      method: "PATCH",
      body: { top },
      accessToken
    }),

  like: (commentId: string, accessToken: string) =>
    apiFetch<{ changed: boolean; liked: boolean }>("/api/v1/action/like", {
      method: "POST",
      body: { entityType: "comment", entityId: commentId },
      accessToken
    }),

  unlike: (commentId: string, accessToken: string) =>
    apiFetch<{ changed: boolean; liked: boolean }>("/api/v1/action/unlike", {
      method: "POST",
      body: { entityType: "comment", entityId: commentId },
      accessToken
    })
};
