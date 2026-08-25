import type {
  Execution,
  ExecutionControl,
  ExecutionEvent,
  ExecutionEventsResponse,
  ExecutionListResponse,
  ExecutionStep,
  ExecutionStepsResponse
} from "@/types/execution";

const baseUrl = (
  (import.meta.env.VITE_GREENBOOK_AGENT_URL as string | undefined)
  ?? "/agent-api"
).replace(/\/$/, "");

type RequestOptions = {
  method?: string;
  token: string;
  body?: unknown;
  signal?: AbortSignal;
};

const request = async <T>(path: string, options: RequestOptions): Promise<T> => {
  const response = await fetch(`${baseUrl}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${options.token}`
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
    credentials: "omit"
  });
  if (!response.ok) {
    const raw = await response.text();
    let message = raw || `Execution 请求失败（${response.status}）`;
    try {
      const parsed = JSON.parse(raw) as { detail?: string; message?: string };
      message = parsed.detail ?? parsed.message ?? message;
    } catch {
      // Keep the response text when the server did not return JSON.
    }
    throw new Error(message);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
};

const isTerminal = (status: string) =>
  ["COMPLETED", "FAILED", "CANCELLED"].includes(status);

const isSettled = (execution: Execution) =>
  isTerminal(execution.status)
  || ["PAUSED", "WAITING_APPROVAL", "WAITING_HUMAN"].includes(execution.status)
  || execution.control_state === "PAUSED";

const parseEventFrame = (frame: string): ExecutionEvent | null => {
  const data = frame
    .split("\n")
    .filter(line => line.startsWith("data:"))
    .map(line => line.slice(5).trim())
    .join("\n");
  if (!data) return null;
  try {
    return JSON.parse(data) as ExecutionEvent;
  } catch {
    return null;
  }
};

export const executionService = {
  list: (token: string, limit = 30, cursor?: string | null, signal?: AbortSignal) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (cursor) params.set("cursor", cursor);
    return request<ExecutionListResponse>(`/api/v1/executions?${params.toString()}`, {
      token,
      signal
    });
  },

  listExecutions: (token: string, limit = 30, cursor?: string | null, signal?: AbortSignal) =>
    executionService.list(token, limit, cursor, signal),

  get: (token: string, executionId: string, signal?: AbortSignal) =>
    request<Execution>(`/api/v1/executions/${encodeURIComponent(executionId)}`, { token, signal }),

  getExecution: (token: string, executionId: string, signal?: AbortSignal) =>
    executionService.get(token, executionId, signal),

  control: (token: string, executionId: string, signal?: AbortSignal) =>
    request<ExecutionControl>(
      `/api/v1/executions/${encodeURIComponent(executionId)}/control`,
      { token, signal }
    ),

  steps: (token: string, executionId: string, signal?: AbortSignal) =>
    request<ExecutionStepsResponse>(
      `/api/v1/executions/${encodeURIComponent(executionId)}/steps`,
      { token, signal }
    ),

  getExecutionSteps: (token: string, executionId: string, signal?: AbortSignal) =>
    executionService.steps(token, executionId, signal),

  events: (token: string, executionId: string, signal?: AbortSignal) =>
    request<ExecutionEventsResponse>(
      `/api/v1/executions/${encodeURIComponent(executionId)}/events`,
      { token, signal }
    ),

  getExecutionEvents: (token: string, executionId: string, signal?: AbortSignal) =>
    executionService.events(token, executionId, signal),

  stream: (
    token: string,
    executionId: string,
    onEvent: (event: ExecutionEvent) => void | Promise<void>,
    signal?: AbortSignal
  ) => streamExecution(token, executionId, onEvent, signal),

  pause: (token: string, executionId: string) =>
    request<Execution>(`/api/v1/executions/${encodeURIComponent(executionId)}/pause`, {
      method: "POST",
      token
    }),

  resume: (token: string, executionId: string) =>
    request<Execution>(`/api/v1/executions/${encodeURIComponent(executionId)}/resume`, {
      method: "POST",
      token
    }),

  cancel: (token: string, executionId: string) =>
    request<Execution>(`/api/v1/executions/${encodeURIComponent(executionId)}/cancel`, {
      method: "POST",
      token
    }),

  retryStep: (token: string, executionId: string, stepId: string) =>
    request<ExecutionStep>(
      `/api/v1/executions/${encodeURIComponent(executionId)}/steps/${encodeURIComponent(stepId)}/retry`,
      { method: "POST", token }
    )
};

export const streamExecution = async (
  token: string,
  executionId: string,
  onEvent: (event: ExecutionEvent) => void | Promise<void>,
  signal?: AbortSignal
): Promise<void> => {
  const response = await fetch(
    `${baseUrl}/api/v1/executions/${encodeURIComponent(executionId)}/stream`,
    {
      headers: { Authorization: `Bearer ${token}` },
      signal,
      credentials: "omit"
    }
  );
  if (!response.ok || !response.body) {
    throw new Error(`Execution stream 连接失败（${response.status}）`);
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
      const event = parseEventFrame(frame);
      if (event) await onEvent(event);
    }
  }
};

export const streamExecutionEvents = streamExecution;

export type AgentRunEvent = {
  event_id: number;
  run_id: string;
  event_type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export const subscribeRunEvents = async (
  token: string,
  runId: string,
  onEvent: (event: AgentRunEvent) => void | Promise<void>,
  signal?: AbortSignal
): Promise<void> => {
  const response = await fetch(
    `${baseUrl}/api/v1/agent/runs/${encodeURIComponent(runId)}/stream`,
    {
      headers: { Authorization: `Bearer ${token}` },
      signal,
      credentials: "omit"
    }
  );
  if (!response.ok || !response.body) return;
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
      const parsed = parseEventFrame(frame);
      if (!parsed) continue;
      await onEvent({
        event_id: 0,
        run_id: runId,
        event_type: String((parsed as { event_type?: string }).event_type || ""),
        payload: (parsed as { payload?: Record<string, unknown> }).payload || {},
        created_at: ""
      });
    }
  }
};

export const waitForExecution = async (
  token: string,
  executionId: string,
  onUpdate: (execution: Execution) => void,
  onEvent?: (event: ExecutionEvent) => void,
  signal?: AbortSignal
): Promise<Execution> => {
  const refresh = async (): Promise<Execution> => {
    const [status, steps, events] = await Promise.all([
      executionService.get(token, executionId, signal),
      executionService.steps(token, executionId, signal),
      executionService.events(token, executionId, signal)
    ]);
    const snapshot: Execution = {
      ...status,
      steps: steps.steps,
      events: events.events
    };
    onUpdate(snapshot);
    return snapshot;
  };

  let current = await refresh();
  if (isSettled(current)) return current;

  const streamController = new AbortController();
  const abortStream = () => streamController.abort();
  signal?.addEventListener("abort", abortStream, { once: true });
  try {
    await streamExecution(
      token,
      executionId,
      async event => {
        onEvent?.(event);
        current = await refresh();
        if (isSettled(current)) streamController.abort();
      },
      streamController.signal
    );
    if (isSettled(current)) return current;
  } catch (error) {
    if (signal?.aborted) throw error;
    // Proxies can buffer or reject SSE. Polling remains the compatibility fallback.
  } finally {
    signal?.removeEventListener("abort", abortStream);
  }

  while (!signal?.aborted) {
    current = await refresh();
    if (isSettled(current)) return current;
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
