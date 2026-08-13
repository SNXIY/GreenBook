import type { CreatorTaskPage } from "@/types/task";

const baseUrl = (
  (import.meta.env.VITE_GREENBOOK_CREATOR_URL as string | undefined)
  ?? "/creator-api"
).replace(/\/$/, "");

type CreatorRequestOptions = {
  method?: string;
  body?: unknown;
};

const request = async <T>(
  path: string,
  token: string,
  options: CreatorRequestOptions = {}
): Promise<T> => {
  const response = await fetch(`${baseUrl}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(options.method === "POST" ? { "Idempotency-Key": crypto.randomUUID() } : {})
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "omit"
  });

  if (!response.ok) {
    const raw = await response.text();
    let message = raw || `创作任务请求失败（${response.status}）`;
    try {
      const parsed = JSON.parse(raw) as { detail?: string; message?: string };
      message = parsed.detail ?? parsed.message ?? message;
    } catch {
      // Keep the original response body.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
};

export const creatorTaskService = {
  list: (token: string) =>
    request<CreatorTaskPage>("/api/v1/creator/tasks?limit=30", token),

  cancel: (token: string, taskId: string, expectedVersion: number) =>
    request(`/api/v1/creator/tasks/${taskId}/cancel`, token, {
      method: "POST",
      body: { expected_version: expectedVersion }
    }),

  retry: (token: string, taskId: string, expectedVersion: number) =>
    request(`/api/v1/creator/tasks/${taskId}/retry`, token, {
      method: "POST",
      body: { expected_version: expectedVersion }
    })
};
