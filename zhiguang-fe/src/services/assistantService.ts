import type {
  AssistantConversation,
  AssistantMessage,
  AssistantMemory,
  AssistantMemoryProfile,
  AssistantEpisode,
  AssistantRun,
  AssistantRunListItem,
  AssistantRunAccepted,
  AssistantScheduledAction
} from "@/types/assistant";

const baseUrl = (
  (import.meta.env.VITE_ASSISTANT_AGENT_URL as string | undefined)
  ?? "/assistant-agent"
).replace(/\/$/, "");

type RequestOptions = {
  method?: string;
  token: string;
  body?: unknown;
  headers?: Record<string, string>;
  signal?: AbortSignal;
};

const request = async <T>(path: string, options: RequestOptions): Promise<T> => {
  const response = await fetch(`${baseUrl}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${options.token}`,
      ...options.headers
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
    credentials: "omit"
  });
  if (!response.ok) {
    const raw = await response.text();
    let message = raw || `助手请求失败（${response.status}）`;
    try {
      const parsed = JSON.parse(raw) as { detail?: string; message?: string };
      message = parsed.detail ?? parsed.message ?? message;
    } catch {
      // Keep the original response.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
};

export const assistantService = {
  createConversation: (
    token: string,
    body: { title?: string; context_post_id?: string; surface: "HOME" | "COMMENT" | "POST" },
    signal?: AbortSignal
  ) => request<AssistantConversation>("/api/v1/assistant/conversations", {
    method: "POST",
    token,
    body,
    signal
  }),

  listConversations: (token: string, contextPostId?: string, signal?: AbortSignal) => {
    const query = contextPostId ? `?context_post_id=${encodeURIComponent(contextPostId)}` : "";
    return request<AssistantConversation[] | { items: AssistantConversation[] }>(
      `/api/v1/assistant/conversations${query}`,
      { token, signal }
    ).then(result => Array.isArray(result) ? result : result.items);
  },

  listMessages: (token: string, conversationId: string, signal?: AbortSignal) =>
    request<AssistantMessage[]>(
      `/api/v1/assistant/conversations/${conversationId}/messages`,
      { token, signal }
    ),

  send: (
    token: string,
    conversationId: string,
    content: string,
    contextPostId?: string,
    contextCommentId?: string
  ) => request<AssistantRunAccepted>(
    `/api/v1/assistant/conversations/${conversationId}/messages`,
    {
      method: "POST",
      token,
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: {
        content,
        context_post_id: contextPostId,
        context_comment_id: contextCommentId,
        client_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai"
      }
    }
  ),

  getRun: (token: string, runId: string, signal?: AbortSignal) =>
    request<AssistantRun>(`/api/v1/assistant/runs/${runId}`, { token, signal }),

  listRuns: (token: string, signal?: AbortSignal) =>
    request<AssistantRunListItem[]>("/api/v1/assistant/runs?limit=30", { token, signal }),

  cancelRun: (token: string, runId: string) =>
    request<AssistantRun>(`/api/v1/assistant/runs/${runId}/cancel`, {
      method: "POST",
      token
    }),

  interruptRun: (token: string, runId: string) =>
    request<AssistantRun>(`/api/v1/assistant/runs/${runId}/interrupt`, {
      method: "POST",
      token
    }),

  resumeRun: (token: string, runId: string) =>
    request<AssistantRun>(`/api/v1/assistant/runs/${runId}/resume`, {
      method: "POST",
      token
    }),

  retryRun: (token: string, runId: string) =>
    request<AssistantRun>(`/api/v1/assistant/runs/${runId}/retry`, {
      method: "POST",
      token
    }),

  listMemories: (token: string, signal?: AbortSignal) =>
    request<AssistantMemory[]>("/api/v1/assistant/memories", { token, signal }),

  saveMemory: (token: string, key: string, value: string) =>
    request<AssistantMemory>("/api/v1/assistant/memories", {
      method: "POST",
      token,
      body: { key, value }
    }),

  deleteMemory: (token: string, memoryId: string) =>
    request<void>(`/api/v1/assistant/memories/${memoryId}`, {
      method: "DELETE",
      token
    }),

  getMemoryProfile: (token: string, signal?: AbortSignal) =>
    request<AssistantMemoryProfile>("/api/v1/assistant/memory/settings", {
      token,
      signal
    }),

  updateMemoryProfile: (
    token: string,
    profile: Pick<AssistantMemoryProfile, "episodic_enabled" | "semantic_enabled">
  ) => request<AssistantMemoryProfile>("/api/v1/assistant/memory/settings", {
    method: "PUT",
    token,
    body: profile
  }),

  listEpisodes: (token: string, signal?: AbortSignal) =>
    request<AssistantEpisode[]>("/api/v1/assistant/memory/episodes?limit=10", {
      token,
      signal
    }),

  deleteEpisode: (token: string, episodeId: string) =>
    request<void>(`/api/v1/assistant/memory/episodes/${episodeId}`, {
      method: "DELETE",
      token
    }),

  clearEpisodes: (token: string) =>
    request<{ deleted: number }>("/api/v1/assistant/memory/episodes", {
      method: "DELETE",
      token
    }),

  decideApproval: (
    token: string,
    runId: string,
    approvalId: string,
    decision: "APPROVE" | "REJECT",
    expectedRunVersion: number
  ) => request<AssistantRun>(
    `/api/v1/assistant/runs/${runId}/approvals/${approvalId}`,
    {
      method: "POST",
      token,
      body: { decision, expected_run_version: expectedRunVersion }
    }
  ),

  scheduledActions: (token: string) =>
    request<AssistantScheduledAction[]>("/api/v1/assistant/scheduled-actions", { token }),

  cancelScheduledAction: (token: string, actionId: string) =>
    request<AssistantScheduledAction>(`/api/v1/assistant/scheduled-actions/${actionId}`, {
      method: "DELETE",
      token
    })
};

export const waitForAssistantRun = async (
  token: string,
  runId: string,
  onUpdate: (run: AssistantRun) => void,
  signal?: AbortSignal
): Promise<AssistantRun> => {
  try {
    const response = await fetch(
      `${baseUrl}/api/v1/assistant/runs/${runId}/events/stream`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal,
        credentials: "omit"
      }
    );
    if (!response.ok || !response.body) {
      throw new Error(`事件流连接失败（${response.status}）`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!signal?.aborted) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        if (!frame.split("\n").some(line => line.startsWith("data:"))) continue;
        onUpdate(await assistantService.getRun(token, runId, signal));
      }
    }
    const current = await assistantService.getRun(token, runId, signal);
    onUpdate(current);
    if (["COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "PAUSED"].includes(current.status)) {
      return current;
    }
  } catch (error) {
    if ((error as DOMException)?.name === "AbortError") throw error;
    // Corporate proxies may buffer SSE; polling remains a compatibility fallback.
  }
  while (!signal?.aborted) {
    const run = await assistantService.getRun(token, runId, signal);
    onUpdate(run);
    if (["COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "PAUSED"].includes(run.status)) return run;
    await new Promise<void>((resolve, reject) => {
      const timer = window.setTimeout(resolve, 800);
      signal?.addEventListener("abort", () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      }, { once: true });
    });
  }
  throw new DOMException("Aborted", "AbortError");
};
