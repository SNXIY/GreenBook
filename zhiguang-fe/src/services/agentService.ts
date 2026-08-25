import type {
  AgentConversation,
  AgentMessage,
  AgentMemoryProfile,
  AgentMemoryRecord,
  AgentRun,
  AgentRunListItem,
  AgentRunAccepted
} from "@/types/agent";
import type {
  SemanticConfirmationControl,
  SemanticConfirmationControlResponse
} from "@/types/semanticConfirmation";
import { getDisplayTimezone } from "@/utils/dateTime";

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

export class AgentApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "AgentApiError";
    this.status = status;
  }
}

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
    throw new AgentApiError(message, response.status);
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
        client_timezone: getDisplayTimezone(),
        command
      }
    }
  ),

  getRun: (token: string, runId: string, signal?: AbortSignal) =>
    request<AgentRun>(`/api/v1/agent/runs/${runId}`, { token, signal }),

  listRuns: (token: string, signal?: AbortSignal) =>
    request<AgentRunListItem[]>("/api/v1/agent/runs?limit=30", { token, signal }),

  getMemoryProfile: (token: string, signal?: AbortSignal) =>
    request<AgentMemoryProfile>("/api/v1/agent/memory/settings", {
      token,
      signal
    }),

  listMemoryRecords: (token: string, signal?: AbortSignal) =>
    request<AgentMemoryRecord[]>("/api/v1/agent/memory/records?limit=20", {
      token,
      signal
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

  controlSemanticConfirmation: (
    token: string,
    taskId: string,
    control: SemanticConfirmationControl,
    signal?: AbortSignal
  ) => request<SemanticConfirmationControlResponse>(
    `/api/v1/agent/tasks/${encodeURIComponent(taskId)}/semantic-confirmation`,
    {
      method: "POST",
      token,
      body: control,
      signal
    }
  )
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
    if (["COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED", "WAITING_APPROVAL", "WAITING_HUMAN", "WAITING_USER", "PAUSED"].includes(run.status)) return run;
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
