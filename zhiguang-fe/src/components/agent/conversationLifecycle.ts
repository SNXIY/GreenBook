import type { AgentConversation } from "@/types/agent";

export type ConversationStorageScope = {
  userId: string;
  tenantId: string;
  surface: string;
  contextPostId?: string | null;
};

const DEFAULT_TITLES = new Set([
  "",
  "new conversation",
  "新会话",
  "greenbook agent"
]);

const encodeScopePart = (value: string | null | undefined): string =>
  encodeURIComponent(String(value ?? "").trim() || "_");

/** Keep the selected Conversation local to one authenticated UI scope. */
export const conversationStorageKey = (scope: ConversationStorageScope): string => [
  "greenbook",
  "agent",
  "selected-conversation",
  encodeScopePart(scope.userId),
  encodeScopePart(scope.tenantId),
  encodeScopePart(scope.surface),
  encodeScopePart(scope.contextPostId)
].join(":");

/** Decode only non-sensitive JWT claims needed to partition local UI state. */
export const tokenTenantId = (token: string | null | undefined): string => {
  if (typeof window === "undefined") return "default";
  if (!token) return "default";
  try {
    const encoded = token.split(".")[1];
    if (!encoded) return "default";
    const normalized = encoded.replace(/-/g, "+").replace(/_/g, "/");
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
    const claims = JSON.parse(window.atob(padded)) as Record<string, unknown>;
    return String(claims.tenant_id ?? claims.tenantId ?? claims.tenant ?? "default");
  } catch {
    return "default";
  }
};

export const readSelectedConversationId = (key: string): string | null => {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
};

export const writeSelectedConversationId = (key: string, conversationId: string): void => {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, conversationId);
  } catch {
    // Local persistence is an enhancement; the durable backend remains authoritative.
  }
};

export const clearSelectedConversationId = (key: string): void => {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(key);
  } catch {
    // Ignore storage quota/privacy-mode failures.
  }
};

/** Deterministic title projection; never invokes an LLM. */
export const titleFromFirstMessage = (content: string, maxLength = 64): string => {
  const normalized = content.trim().replace(/\s+/g, " ");
  if (!normalized) return "";
  if (normalized.length <= maxLength) return normalized;
  return `${normalized.slice(0, Math.max(1, maxLength - 1)).trimEnd()}…`;
};

export const hasCustomConversationTitle = (conversation: AgentConversation | null): boolean =>
  Boolean(conversation?.title && !DEFAULT_TITLES.has(conversation.title.trim().toLowerCase()));

export const isConversationSelectionCurrent = (
  activeConversationId: string | null,
  activeGeneration: number,
  conversationId: string,
  generation: number
): boolean => activeConversationId === conversationId && activeGeneration === generation;
