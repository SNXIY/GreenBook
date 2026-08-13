import type {
  AgentConversation,
  AgentMessage,
  AgentMemory,
  AgentMemoryProfile,
  AgentEpisode,
  AgentRun,
  AgentRunListItem,
  AgentRunAccepted,
  AgentScheduledAction
} from "@/types/agent";

const baseUrl = (
  (import.meta.env.VITE_GREENBOOK_AGENT_URL as string | undefined)
  ?? "/agent-api"
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
    let message = raw || `Agent请求失败（${response.status}）`;
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

export const agentService = {
  createConversation: (
    token: string,
    body: { title?: string; context_post_id?: string; surface: "HOME" | "COMMENT" | "POST" },
    signal?: AbortSignal
  ) => request<AgentConversation>("/api/v1/agent/conversations", {
    method: "POST",
    token,
    body,
    signal
  }),

  listConversations: (token: string, contextPostId?: string, signal?: AbortSignal) => {
    const query = contextPostId ? `?context_post_id=${encodeURIComponent(contextPostId)}` : "";
    return request<AgentConversation[] | { items: AgentConversation[] }>(
      `/api/v1/agent/conversations${query}`,
      { token, signal }
    ).then(result => Array.isArray(result) ? result : result.items);
  },

  listMessages: (token: string, conversationId: string, signal?: AbortSignal) =>
    request<AgentMessage[]>(
      `/api/v1/agent/conversations/${conversationId}/messages`,
      { token, signal }
    ),

  send: (
    token: string,
    conversationId: string,
    content: string,
    contextPostId?: string,
    contextCommentId?: string,
    command?: Record<string, unknown>
  ) => request<AgentRunAccepted>(
    `/api/v1/agent/conversations/${conversationId}/messages`,
    {
      method: "POST",
      token,
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: {
        content,
        context_post_id: contextPostId,
        context_comment_id: contextCommentId,
        client_timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai",
        command
      }
    }
  ),

  getRun: (token: string, runId: string, signal?: AbortSignal) =>
    request<AgentRun>(`/api/v1/agent/runs/${runId}`, { token, signal }),

  listRuns: (token: string, signal?: AbortSignal) =>
    request<AgentRunListItem[]>("/api/v1/agent/runs?limit=30", { token, signal }),

  cancelRun: (token: string, runId: string) =>
    request<AgentRun>(`/api/v1/agent/runs/${runId}/cancel`, {
      method: "POST",
      token
    }),

  interruptRun: (token: string, runId: string) =>
    request<AgentRun>(`/api/v1/agent/runs/${runId}/interrupt`, {
      method: "POST",
      token
    }),

  resumeRun: (token: string, runId: string) =>
    request<AgentRun>(`/api/v1/agent/runs/${runId}/resume`, {
      method: "POST",
      token
    }),

  retryRun: (token: string, runId: string) =>
    request<AgentRun>(`/api/v1/agent/runs/${runId}/retry`, {
      method: "POST",
      token
    }),

  listMemories: (token: string, signal?: AbortSignal) =>
    request<AgentMemory[]>("/api/v1/agent/memories", { token, signal }),

  saveMemory: (token: string, key: string, value: string) =>
    request<AgentMemory>("/api/v1/agent/memories", {
      method: "POST",
      token,
      body: { key, value }
    }),

  deleteMemory: (token: string, memoryId: string) =>
    request<void>(`/api/v1/agent/memories/${memoryId}`, {
      method: "DELETE",
      token
    }),

  getMemoryProfile: (token: string, signal?: AbortSignal) =>
    request<AgentMemoryProfile>("/api/v1/agent/memory/settings", {
      token,
      signal
    }),

  updateMemoryProfile: (
    token: string,
    profile: Pick<AgentMemoryProfile, "episodic_enabled" | "semantic_enabled">
  ) => request<AgentMemoryProfile>("/api/v1/agent/memory/settings", {
    method: "PUT",
    token,
    body: profile
  }),

  listEpisodes: (token: string, signal?: AbortSignal) =>
    request<AgentEpisode[]>("/api/v1/agent/memory/episodes?limit=10", {
      token,
      signal
    }),

  deleteEpisode: (token: string, episodeId: string) =>
    request<void>(`/api/v1/agent/memory/episodes/${episodeId}`, {
      method: "DELETE",
      token
    }),

  clearEpisodes: (token: string) =>
    request<{ deleted: number }>("/api/v1/agent/memory/episodes", {
      method: "DELETE",
      token
    }),

  decideApproval: (
    token: string,
    runId: string,
    approvalId: string,
    decision: "APPROVE" | "REJECT",
    expectedRunVersion: number
  ) => request<AgentRun>(
    `/api/v1/agent/runs/${runId}/approvals/${approvalId}`,
    {
      method: "POST",
      token,
      body: { decision, expected_run_version: expectedRunVersion }
    }
  ),

  scheduledActions: (token: string) =>
    request<AgentScheduledAction[]>("/api/v1/agent/scheduled-actions", { token }),

  cancelScheduledAction: (token: string, actionId: string) =>
    request<AgentScheduledAction>(`/api/v1/agent/scheduled-actions/${actionId}`, {
      method: "DELETE",
      token
    })
};

export const waitForAgentRun = async (
  token: string,
  runId: string,
  onUpdate: (run: AgentRun) => void,
  signal?: AbortSignal
): Promise<AgentRun> => {
  // Run is a history projection. The canonical live stream belongs to
  // Execution and is handled by waitForExecution when an execution_id exists.
  // Compatibility runs without an Execution are therefore polled directly.
  while (!signal?.aborted) {
    const run = await agentService.getRun(token, runId, signal);
    onUpdate(run);
    if (["COMPLETED", "FAILED", "CANCELLED", "WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(run.status)) return run;
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
