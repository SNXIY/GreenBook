import type {
  UserActivityEvent,
  UserActivityListResponse
} from "@/types/userActivity";

const baseUrl = (
  (import.meta.env.VITE_GREENBOOK_AGENT_URL as string | undefined)
  ?? "/agent-api"
).replace(/\/$/, "");

const delay = (milliseconds: number, signal?: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = globalThis.setTimeout(resolve, milliseconds);
    signal?.addEventListener("abort", () => {
      globalThis.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }, { once: true });
  });

const parseActivityFrame = (frame: string): UserActivityEvent | null => {
  const eventType = frame
    .split("\n")
    .find(line => line.startsWith("event:"))
    ?.slice("event:".length)
    .trim();
  if (eventType && eventType !== "user_activity") return null;
  const data = frame
    .split("\n")
    .filter(line => line.startsWith("data:"))
    .map(line => line.slice("data:".length).trim())
    .join("\n");
  if (!data) return null;
  try {
    const parsed = JSON.parse(data) as UserActivityEvent;
    return parsed.activity_id && Number.isFinite(parsed.sequence) ? parsed : null;
  } catch {
    return null;
  }
};

/** Idempotent client merge for SSE replay plus polling fallback. */
export const mergeUserActivityEvents = (
  previous: UserActivityEvent[],
  incoming: UserActivityEvent[]
): UserActivityEvent[] => {
  const byId = new Map(previous.map(item => [item.activity_id, item]));
  for (const item of incoming) byId.set(item.activity_id, item);
  return [...byId.values()].sort((left, right) => left.sequence - right.sequence);
};

export const userActivityService = {
  list: async (
    token: string,
    conversationId: string,
    afterSequence = 0,
    signal?: AbortSignal
  ): Promise<UserActivityListResponse> => {
    const params = new URLSearchParams({ after_sequence: String(Math.max(0, afterSequence)) });
    const response = await fetch(
      `${baseUrl}/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/activities?${params.toString()}`,
      {
        headers: { Authorization: `Bearer ${token}` },
        signal,
        credentials: "omit"
      }
    );
    if (!response.ok) {
      throw new Error(`User activity request failed (${response.status})`);
    }
    return response.json() as Promise<UserActivityListResponse>;
  }
};

/**
 * Authenticated SSE with cursor-based replay and reconnect.  The caller must
 * still dedupe by activity_id because polling is deliberately used as a
 * transport fallback by the panel.
 */
export const subscribeUserActivities = async (
  token: string,
  conversationId: string,
  onEvent: (event: UserActivityEvent) => void | Promise<void>,
  options: { afterSequence?: number; signal?: AbortSignal } = {}
): Promise<void> => {
  const signal = options.signal;
  let cursor = Math.max(0, options.afterSequence ?? 0);
  let reconnectAttempt = 0;
  const delivered = new Set<string>();

  while (!signal?.aborted) {
    try {
      const params = new URLSearchParams({ after_sequence: String(cursor) });
      const response = await fetch(
        `${baseUrl}/api/v1/agent/conversations/${encodeURIComponent(conversationId)}/activities/stream?${params.toString()}`,
        {
          headers: {
            Authorization: `Bearer ${token}`,
            "Last-Event-ID": String(cursor)
          },
          signal,
          credentials: "omit"
        }
      );
      if (!response.ok || !response.body) {
        throw new Error(`User activity stream unavailable (${response.status})`);
      }

      reconnectAttempt = 0;
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
          const event = parseActivityFrame(frame);
          if (!event) continue;
          cursor = Math.max(cursor, event.sequence);
          if (delivered.has(event.activity_id)) continue;
          delivered.add(event.activity_id);
          // A bounded dedupe window protects a very long-lived panel without
          // sacrificing reconnect safety (the server cursor is authoritative).
          if (delivered.size > 1000) delivered.clear();
          await onEvent(event);
        }
      }
    } catch (error) {
      if (signal?.aborted || (error as DOMException)?.name === "AbortError") {
        break;
      }
    }

    reconnectAttempt += 1;
    const wait = Math.min(5_000, 250 * 2 ** Math.min(reconnectAttempt, 5));
    await delay(wait, signal);
  }
};

export { parseActivityFrame };
