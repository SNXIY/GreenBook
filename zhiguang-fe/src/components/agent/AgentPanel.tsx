import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import AgentMarkdown from "@/components/content/AgentMarkdown";
import {
  AgentIcon,
  CheckIcon,
  CloseIcon,
  SearchIcon,
  SendIcon
} from "@/components/icons/Icon";
import { AgentApiError, agentService, waitForAgentRun } from "@/services/agentService";
import { executionService, waitForExecution } from "@/services/executionService";
import type {
  AgentConversation,
  AgentMemoryProfile,
  AgentMemoryRecord,
  AgentMessage,
  AgentExecutionResultPart,
  AgentRun,
  AgentMessagePart,
  AgentToolPart,
  AgentTargetClarificationPart,
  AgentUnderstanding,
  AgentUserFacingInteractionPart
} from "@/types/agent";
import type { Execution } from "@/types/execution";
import type { UserFacingTargetClarification } from "./userFacingResult";
import { formatBusinessDateTime, getDisplayTimezone } from "@/utils/dateTime";
import {
  canSubmitNaturalLanguage,
  isComposerDisabled,
  type ComposerState
} from "./agentComposerState";
import AgentResultGroup from "./AgentResultCards";
import {
  AgentApprovalCard,
  // LEGACY_FALLBACK: the ordinary panel keeps these branches disabled below.
  AgentExecutionActivityCard,
  AgentExecutionActivityGroup,
  AgentRunActivityCard
} from "./AgentActivityCards";
import { subscribeRunEvents } from "../../services/executionService";
import type { AgentRunEvent } from "../../services/executionService";
import { RUN_EVENT } from "../../services/runEvents";
import {
  capabilityActiveLabel,
  projectAgentRunToUserFacingInteraction,
  projectAgentRunArtifactsToUserFacingInteractions,
  projectPendingApprovalFallback,
  projectAgentMessageToUserFacingInteractions,
  dedupeTerminalAgentMessages,
  projectUserFacingInteractionPart,
  projectTargetClarification,
  isTargetClarificationResolved,
  userFacingDisplayText,
  userFacingMessage,
  userFacingStatusLabel
} from "./userFacingResult";
import {
  mergeUserActivityEvents,
  subscribeUserActivities,
  userActivityService
} from "@/services/userActivityService";
import type { UserActivityEvent } from "@/types/userActivity";
import {
  buildSemanticConfirmationControl,
  projectSemanticConfirmation,
  selectLatestSemanticConfirmationEvents,
  semanticConfirmationErrorMessage,
  semanticConfirmationKey,
  type SemanticConfirmationPayload,
  type SemanticConfirmationViewState
} from "@/types/semanticConfirmation";
import UserActivityCluster from "./UserActivityCluster";
import SemanticConfirmationCard from "./SemanticConfirmationCard";
import styles from "./AgentPanel.module.css";

type Props = {
  open: boolean;
  onClose: () => void;
  contextPostId?: string;
  surface?: "HOME" | "POST";
};

const ACTIVE_RUN_STATUSES = new Set([
  "QUEUED",
  "RUNNING",
  "RETRYING",
  "WAITING_DEPENDENCY",
  "WAITING_LANE",
  "WAITING_APPROVAL",
  "WAITING_HUMAN",
  "WAITING_USER",
  "PAUSED"
]);

/** User-facing label for a restored run: waiting states are NOT "处理中". */
function restoredRunTitle(item: { status?: string | null; goal?: string | null }): string {
  return runItemTitle(item);
}

/** Label for one concurrent run card by status (waiting ≠ 处理中). */
function runItemTitle(item: { status?: string | null; title?: string | null; goal?: string | null }): string {
  const status = item.status || "";
  if (status === "WAITING_USER" || status === "WAITING_HUMAN") {
    return "等待你确认";
  }
  if (status === "WAITING_APPROVAL") {
    return "等待审批";
  }
  if (status === "PAUSED") {
    return "已暂停";
  }
  return item.title || userFacingMessage(item.goal || "") || "正在处理一项事情";
}

type ConcurrentRunView = {
  title: string;
  status: string;
  error?: string | null;
  follow_up_of?: string | null;
};

/** @deprecated LEGACY_FALLBACK for historical Run-event consumers only. */
type ConcurrentRunActivity = {
  current: string | null;
  done: Array<{ title: string; count?: number; runAt?: string }>;
};

/** One queued mid-turn follow-up, shown as a hint on its parent card. */
type FollowUpHint = {
  followUpRunId: string;
  message: string;
};

const agentMessageCount = (items: AgentMessage[]) =>
  items.filter(item => item.role === "assistant").length;

const isExecutionResultPart = (
  part: AgentMessagePart
): part is AgentExecutionResultPart => part.type === "execution_result";

const isUserFacingInteractionPart = (
  part: AgentMessagePart
): part is AgentUserFacingInteractionPart => part.type === "user_facing_interaction";

const isTerminalExecution = (status: string): boolean =>
  ["COMPLETED", "FAILED", "CANCELLED"].includes(String(status).toUpperCase());

const isApprovalPending = (execution?: Execution | null, run?: AgentRun | null): boolean =>
  ["WAITING_APPROVAL", "WAITING_HUMAN"].includes(String(execution?.status).toUpperCase())
  || ["WAITING_APPROVAL", "WAITING_HUMAN"].includes(String(run?.status).toUpperCase())
  || ["PENDING", "WAITING_APPROVAL", "WAITING_HUMAN"].includes(String(run?.approval?.status).toUpperCase());

const upsertExecution = (items: Execution[], next: Execution): Execution[] => {
  const current = items.findIndex(item => item.execution_id === next.execution_id);
  if (current < 0) return [...items, next];
  return items.map((item, index) => index === current ? next : item);
};

const refreshMessagesAfterExecution = async (
  token: string,
  conversationId: string,
  previousAgentCount: number,
  expectedExecutionIds: string[] = [],
  signal?: AbortSignal
): Promise<{ messages: AgentMessage[]; projected: boolean }> => {
  let latest: AgentMessage[] = [];
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    latest = await agentService.listMessages(token, conversationId, signal);
    const projected = expectedExecutionIds.length
      ? latest.some(message => (message.parts || []).some(part =>
        isExecutionResultPart(part)
        && expectedExecutionIds.includes(part.execution.execution_id)
      ))
      : agentMessageCount(latest) > previousAgentCount;
    if (projected) {
      return { messages: latest, projected: true };
    }
    await new Promise(resolve => window.setTimeout(resolve, 200));
  }
  return { messages: latest, projected: false };
};

const AgentPanel = ({ open, onClose, contextPostId, surface = "HOME" }: Props) => {
  const { tokens, isLoading: authLoading } = useAuth();
  const token = tokens?.accessToken;
  const [conversation, setConversation] = useState<AgentConversation | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [content, setContent] = useState("");
  const [run, setRun] = useState<AgentRun | null>(null);
  const [concurrentRuns, setConcurrentRuns] = useState<Record<string, ConcurrentRunView>>({});
  // LEGACY_FALLBACK: no Phase 2 ordinary-user rendering reads this state.
  const [concurrentActivities, setConcurrentActivities] = useState<Record<string, ConcurrentRunActivity>>({});
  const [userActivities, setUserActivities] = useState<UserActivityEvent[]>([]);
  const [semanticConfirmationStates, setSemanticConfirmationStates] = useState<
    Record<string, SemanticConfirmationViewState>
  >({});
  const [semanticConfirmationErrors, setSemanticConfirmationErrors] = useState<Record<string, string>>({});
  const [resolvedApprovalActivityIds, setResolvedApprovalActivityIds] = useState<
    Record<string, "APPROVED" | "REJECTED" | "COMPLETED">
  >({});
  // Mid-turn injection: keyed by the parent run id; rendered as a hint on the
  // parent card ("已收到你的补充…") instead of a second parallel card.
  const [followUpHints, setFollowUpHints] = useState<Record<string, FollowUpHint>>({});
  // First-step visibility: what the agent understood, shown before it keeps
  // executing so a wrong understanding can be stopped early.
  const [understanding, setUnderstanding] = useState<AgentUnderstanding | null>(null);
  const [execution, setExecution] = useState<Execution | null>(null);
  const [executions, setExecutions] = useState<Execution[]>([]);
  // LEGACY_FALLBACK state for disabled Run/Execution heuristic branches.
  const pendingCapability: string | null = null;
  const runActivity = {
    current: null as string | null,
    done: [] as Array<{ title: string; count?: number }>
  };
  const [memoryProfile, setMemoryProfile] = useState<AgentMemoryProfile | null>(null);
  const [memoryRecords, setMemoryRecords] = useState<AgentMemoryRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [composerState, setComposerState] = useState<ComposerState>("READY");
  const [projectionPending, setProjectionPending] = useState(false);
  const [runsHydrated, setRunsHydrated] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const runControllerRef = useRef<AbortController | null>(null);
  const runControllersRef = useRef<Map<string, AbortController>>(new Map());
  const activityCursorRef = useRef(0);
  const semanticActionKeysRef = useRef<Set<string>>(new Set());
  const semanticModifySupersededKeysRef = useRef<Set<string>>(new Set());

  const mergeUserActivities = useCallback((incoming: UserActivityEvent[]) => {
    if (!incoming.length) return;
    activityCursorRef.current = Math.max(
      activityCursorRef.current,
      ...incoming.map(item => item.sequence)
    );
    setUserActivities(previous => mergeUserActivityEvents(previous, incoming));
  }, []);

  useEffect(() => {
    if (!open || !token || authLoading) return;
    const controller = new AbortController();
    const prepare = async () => {
      setLoading(true);
      setError(null);
      setProjectionPending(false);
      setRunsHydrated(false);
      try {
        const existing = await agentService.listConversations(
          token,
          contextPostId,
          controller.signal
        );
        const current = existing[0] ?? await agentService.createConversation(token, {
          surface,
          context_post_id: contextPostId
        }, controller.signal);
        if (controller.signal.aborted) return;
        setConversation(current);
        const [nextMessages, nextMemoryProfile] = await Promise.all([
          agentService.listMessages(token, current.conversation_id, controller.signal),
          agentService.getMemoryProfile(token, controller.signal)
        ]);
        setMessages(nextMessages);
        setMemoryProfile(nextMemoryProfile);
        try {
          const records = await agentService.listMemoryRecords(token, controller.signal);
          if (!controller.signal.aborted) setMemoryRecords(records ?? []);
        } catch {
          // Memory records are optional UI enrichment; failure must not block chat.
        }

        // The execution card is transient UI state. Restore it from the
        // conversation's active run so reopening the panel does not leave
        // only the optimistic user message visible.
        const activeRuns = (await agentService.listRuns(token, controller.signal))
          .filter(item => item.conversation_id === current.conversation_id)
          .filter(item => ACTIVE_RUN_STATUSES.has(item.status))
          .sort((left, right) =>
            (right.created_at || "").localeCompare(left.created_at || "")
          );
        const restoredViews = Object.fromEntries(activeRuns.map(item => [
          item.run_id,
          {
            title: restoredRunTitle(item),
            status: item.status,
            error: item.error,
            follow_up_of: item.follow_up_of
          }
        ]));
        if (!controller.signal.aborted) {
          setConcurrentRuns(restoredViews);
          setRunsHydrated(true);
        }
        // Mid-turn follow-ups whose parent is still active restore as hints on
        // the parent card; standalone follow-ups (parent already terminal)
        // stay as their own card because they start executing immediately.
        const restoredHints: Record<string, FollowUpHint> = {};
        const restoredIds = new Set(activeRuns.map(item => item.run_id));
        for (const item of activeRuns) {
          if (!item.follow_up_of) continue;
          if (restoredIds.has(item.follow_up_of)) {
            restoredHints[item.follow_up_of] = {
              followUpRunId: item.run_id,
              message: item.goal || "补充指令"
            };
          }
        }
        if (!controller.signal.aborted && Object.keys(restoredHints).length) {
          setFollowUpHints(previous => ({ ...previous, ...restoredHints }));
        }
        const activeRun = activeRuns[0];
        if (activeRun && !controller.signal.aborted) {
          const activeStatus = activeRun.status as string;
          if (activeStatus === "WAITING_APPROVAL" || activeStatus === "WAITING_HUMAN") {
            if (activeRun.execution_id) {
              const snapshot = await executionService.get(token, activeRun.execution_id, controller.signal);
              setExecution(snapshot);
              setExecutions([snapshot]);
            }
            try {
              setRun(await agentService.getRun(token, activeRun.run_id, controller.signal));
            } catch {
              // The activity snapshot can still be restored from the execution projection.
            }
          } else if (activeStatus === "PAUSED" && activeRun.execution_id) {
            const snapshot = await executionService.get(token, activeRun.execution_id, controller.signal);
            setExecution(snapshot);
            setExecutions([snapshot]);
          } else if (activeRun.execution_id) {
            void waitForExecution(
              token,
              activeRun.execution_id,
              snapshot => {
                setExecution(snapshot);
                if (isTerminalExecution(snapshot.status)) setProjectionPending(true);
              },
              undefined,
              controller.signal
            ).then(async completed => {
              if (controller.signal.aborted) return;
              setExecutions([completed]);
              if (["COMPLETED", "FAILED", "CANCELLED"].includes(completed.status)) {
                setProjectionPending(true);
                const refreshed = await refreshMessagesAfterExecution(
                  token,
                  current.conversation_id,
                  agentMessageCount(nextMessages),
                  [completed.execution_id],
                  controller.signal
                );
                setMessages(refreshed.messages);
                setRun(null);
                if (refreshed.projected) {
                  setExecution(null);
                  setExecutions([]);
                  setProjectionPending(false);
                } else {
                  setExecution(completed);
                }
              }
            }).catch(caught => {
              if (!controller.signal.aborted && (caught as DOMException)?.name !== "AbortError") {
                setError(friendlyClientError(caught));
              }
            });
          } else {
            try {
              setRun(await agentService.getRun(token, activeRun.run_id, controller.signal));
            } catch {
              setError("任务状态暂时无法恢复，请稍后刷新对话。");
              return;
            }
            void waitForAgentRun(
              token,
              activeRun.run_id,
              setRun,
              controller.signal
            ).then(async completed => {
              if (controller.signal.aborted) return;
              if (completed.status === "COMPLETED" || completed.status === "CANCELLED") {
                setMessages(await agentService.listMessages(
                  token,
                  current.conversation_id,
                  controller.signal
                ));
                setRun(null);
              } else {
                setRun(completed);
              }
            }).catch(caught => {
              if (!controller.signal.aborted && (caught as DOMException)?.name !== "AbortError") {
                setError(friendlyClientError(caught));
              }
            });
          }
        }
      } catch (caught) {
        if (!controller.signal.aborted) {
          setError(friendlyClientError(caught));
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void prepare();
    return () => controller.abort();
  }, [authLoading, contextPostId, open, surface, token]);

  // User progress comes from the durable Activity projection, not Run/Event/
  // Step heuristics.  SSE is the low-latency path; cursor polling is a
  // deliberate fallback for proxies that buffer or disconnect streams.
  useEffect(() => {
    if (!open || !token || !conversation) return;
    const controller = new AbortController();
    activityCursorRef.current = 0;
    setUserActivities([]);
    setSemanticConfirmationStates({});
    setSemanticConfirmationErrors({});
    semanticActionKeysRef.current.clear();
    semanticModifySupersededKeysRef.current.clear();
    setResolvedApprovalActivityIds({});

    const sync = async () => {
      try {
        const response = await userActivityService.list(
          token,
          conversation.conversation_id,
          activityCursorRef.current,
          controller.signal
        );
        mergeUserActivities(response.items);
      } catch {
        // The SSE reconnect loop and next poll will recover.  A transport
        // failure must not be rendered as a failed business operation.
      }
    };

    void sync();
    void subscribeUserActivities(
      token,
      conversation.conversation_id,
      event => mergeUserActivities([event]),
      { signal: controller.signal }
    ).catch(() => {
      // Abort and polling fallback are both expected terminal paths here.
    });
    const pollingFallback = window.setInterval(() => void sync(), 4_000);
    return () => {
      controller.abort();
      window.clearInterval(pollingFallback);
    };
  }, [conversation, mergeUserActivities, open, token]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    if (window.matchMedia("(hover: hover) and (pointer: fine)").matches) {
      window.setTimeout(() => inputRef.current?.focus(), 180);
    }
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      runControllerRef.current?.abort();
      runControllersRef.current.forEach(controller => controller.abort());
      runControllersRef.current.clear();
    };
  }, [onClose, open]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [messages, userActivities]);

  useEffect(() => {
    const trackedExecutionIds = new Set([
      ...(execution?.execution_id ? [execution.execution_id] : []),
      ...executions.map(item => item.execution_id)
    ]);
    if (!open || !projectionPending || !token || !conversation || !trackedExecutionIds.size) return;

    let cancelled = false;
    let syncing = false;
    const reconcile = async () => {
      if (cancelled || syncing) return;
      syncing = true;
      try {
        const latest = await agentService.listMessages(token, conversation.conversation_id);
        if (cancelled) return;
        const projectedExecutionIds = new Set(
          latest.flatMap(message => (message.parts || [])
            .filter(isExecutionResultPart)
            .map(part => part.execution.execution_id))
        );
        const resolvedExecutionIds = [...trackedExecutionIds].filter(id => projectedExecutionIds.has(id));
        if (!resolvedExecutionIds.length) return;
        setMessages(latest);
        setExecution(current => current && projectedExecutionIds.has(current.execution_id) ? null : current);
        setExecutions(current => current.filter(item => !projectedExecutionIds.has(item.execution_id)));
        if ([...trackedExecutionIds].every(id => projectedExecutionIds.has(id))) {
          setProjectionPending(false);
        }
      } catch {
        // The next reconciliation pass will pick up the projection once it is available.
      } finally {
        syncing = false;
      }
    };

    void reconcile();
    const timer = window.setInterval(() => void reconcile(), 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [conversation, execution?.execution_id, executions, open, projectionPending, token]);

  // ── mid-turn follow-up (nanobot-style injection) ─────────────────────
  // A message sent while a working Run is active is queued behind it
  // (payload.follow_up_of). It renders as a hint on the parent card and only
  // starts executing once the parent reaches a terminal state.

  /**
   * LEGACY_FALLBACK: historical Run-event heuristic retained only for old
   * callers during rollout. New ordinary-user paths subscribe exclusively to
   * UserActivityEvent below and must not call this function.
   */
  const applyRunActivity = (runId: string, event: AgentRunEvent) => {
    if (event.event_type === RUN_EVENT.SEMANTIC_ACTION) {
      const phase = String(event.payload?.phase || "STARTED");
      if (phase === "SUCCEEDED" || phase === "FAILED") {
        setConcurrentActivities(previous => ({
          ...previous,
          [runId]: {
            ...(previous[runId] || { current: null, done: [] }),
            current: null
          }
        }));
        return;
      }
      const action = String(event.payload?.semantic_action || "");
      const label = action ? capabilityActiveLabel(action) || action : "";
      setConcurrentRuns(previous => ({
        ...previous,
        [runId]: {
          ...(previous[runId] || { title: "正在处理一项事情", status: "RUNNING" }),
          title: label || previous[runId]?.title || "正在处理一项事情",
          status: "RUNNING"
        }
      }));
      setConcurrentActivities(previous => ({
        ...previous,
        [runId]: {
          ...(previous[runId] || { current: null, done: [] }),
          current: label || previous[runId]?.current || null
        }
      }));
      return;
    }
    if (event.event_type === RUN_EVENT.PARTIAL_RESULT) {
      const title = String(event.payload?.title || "");
      const count = event.payload?.count;
      const runAt = String(event.payload?.run_at || "");
      setConcurrentActivities(previous => ({
        ...previous,
        [runId]: {
          current: null,
          done: [...(previous[runId]?.done || []), {
            title,
            count: typeof count === "number" ? count : undefined,
            runAt: runAt || undefined
          }]
        }
      }));
      return;
    }
    if (event.event_type === RUN_EVENT.FOLLOW_UP_QUEUED) {
      const followUpRunId = String(event.payload?.follow_up_run_id || "");
      const message = String(event.payload?.message || "");
      if (followUpRunId) {
        setFollowUpHints(previous => ({
          ...previous,
          [runId]: { followUpRunId, message }
        }));
      }
    }
  };

  const attachFollowUpRun = (followUpRunId: string) => {
    if (!token || !conversation) return;
    if (runControllersRef.current.has(followUpRunId)) return;
    const controller = new AbortController();
    runControllersRef.current.set(followUpRunId, controller);
    setConcurrentRuns(previous => ({
      ...previous,
      [followUpRunId]: { title: "处理你的补充指令…", status: "ACCEPTED", error: null }
    }));
    setConcurrentActivities(previous => ({
      ...previous,
      [followUpRunId]: { current: null, done: [] }
    }));
    // User progress is delivered by the conversation-scoped Activity stream;
    // do not attach a per-Run heuristic subscription for new follow-ups.
    void waitForAgentRun(
      token,
      followUpRunId,
      next => setConcurrentRuns(previous => ({
        ...previous,
        [followUpRunId]: {
          ...(previous[followUpRunId] || { title: "处理你的补充指令…" }),
          status: next.status,
          error: next.error
        }
      })),
      controller.signal
    ).then(async completed => {
      if (controller.signal.aborted) return;
      if (completed.status === "FAILED") {
        setError(null);
        setConcurrentRuns(previous => ({
          ...previous,
          [followUpRunId]: {
            ...(previous[followUpRunId] || { title: "这项事情" }),
            status: completed.status,
            error: completed.error
          }
        }));
        return;
      }
      if (["WAITING_APPROVAL", "WAITING_HUMAN", "WAITING_USER", "PAUSED"].includes(completed.status)) {
        // A waiting run has already produced the durable assistant message
        // (for example a target clarification). Refresh it before returning
        // so the live panel can render the actual user action, not only the
        // activity placeholder.
        try {
          setMessages(await agentService.listMessages(
            token,
            conversation.conversation_id,
            controller.signal
          ));
        } catch {
          // Keep the durable run/activity projection if message sync is
          // temporarily unavailable; the next panel sync can recover it.
        }
        setRun(completed);
        return;
      }
      setMessages(await agentService.listMessages(
        token,
        conversation.conversation_id,
        controller.signal
      ));
      setConcurrentRuns(previous => {
        const next = { ...previous };
        delete next[followUpRunId];
        return next;
      });
      setConcurrentActivities(previous => {
        const next = { ...previous };
        delete next[followUpRunId];
        return next;
      });
      maybeAttachFollowUps([followUpRunId]);
    }).catch(caught => {
      if ((caught as DOMException)?.name !== "AbortError") {
        setError(friendlyClientError(caught));
      }
    }).finally(() => {
      runControllersRef.current.delete(followUpRunId);
    });
  };

  const maybeAttachFollowUps = (explicitTerminalParents: string[] = []) => {
    const terminal = new Set(explicitTerminalParents);
    for (const [parentRunId, hint] of Object.entries(followUpHints)) {
      if (!terminal.has(parentRunId) && concurrentRuns[parentRunId]) continue;
      setFollowUpHints(previous => {
        const next = { ...previous };
        delete next[parentRunId];
        return next;
      });
      void attachFollowUpRun(hint.followUpRunId);
    }
  };

  const registerFollowUpHint = (parentRunId: string, followUpRunId: string, message: string) => {
    if (!concurrentRuns[parentRunId]) {
      // The parent already finished (or its card was dismissed); start the
      // queued follow-up immediately as its own card.
      void attachFollowUpRun(followUpRunId);
      return;
    }
    setFollowUpHints(previous => ({
      ...previous,
      [parentRunId]: { followUpRunId, message }
    }));
  };

  // Restored mid-turn hints: the parent card restored from listRuns has a
  // frozen snapshot; poll the durable run list and start the follow-up once
  // the parent reaches a terminal state (including panel reopen mid-work).
  useEffect(() => {
    if (!open || !token || !Object.keys(followUpHints).length) return;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const runs = await agentService.listRuns(token, controller.signal);
        const terminalParents = Object.keys(followUpHints).filter(parentRunId => {
          const parent = runs.find(item => item.run_id === parentRunId);
          return !parent || !ACTIVE_RUN_STATUSES.has(parent.status);
        });
        if (!terminalParents.length) return;
        setConcurrentRuns(previous => {
          const next = { ...previous };
          for (const parentRunId of terminalParents) delete next[parentRunId];
          return next;
        });
        maybeAttachFollowUps(terminalParents);
      } catch {
        // Transient; the next poll retries.
      }
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 2000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [open, token, followUpHints]);

  const send = async (
    suggestion?: string,
    commandOverride?: Record<string, unknown>
  ) => {
    const prompt = (suggestion ?? content).trim();
    if (!token || !conversation) return;
    if (!canSubmitNaturalLanguage(prompt, composerState, true)) return;
    const approvalPendingBeforeSend = isApprovalPending(execution, run);
    const optimistic: AgentMessage = {
      message_id: crypto.randomUUID(),
      role: "user",
      content: prompt,
      parts: [],
      created_at: new Date().toISOString()
    };
    const previousAgentCount = agentMessageCount(messages);
    setMessages(previous => [...previous, optimistic]);
    setContent("");
    setUnderstanding(null);
    if (!approvalPendingBeforeSend) {
      if (!Object.keys(concurrentRuns).length) {
        setRun(null);
        setExecution(null);
        setExecutions([]);
      }
    } else if (run?.approval || isApprovalPending(null, run)) {
      // Keep the approval projection visible while a natural-language
      // follow-up is being submitted. The approval buttons are disabled by
      // `loading`, but the composer remains a separate interaction surface.
      setExecution(null);
      setExecutions([]);
    }
    setProjectionPending(false);
    setError(null);
    setComposerState("SUBMITTING");
    setLoading(true);
    const controller = new AbortController();
    try {
      const accepted = await agentService.send(
        token,
        conversation.conversation_id,
        prompt,
        contextPostId,
        undefined,
        commandOverride
      );
      // Show the first decided semantic action as an immediate business
      // activity while the execution snapshot is still on its way.
      const executionIds = accepted.execution_ids?.length
        ? accepted.execution_ids
        : accepted.execution_id ? [accepted.execution_id] : [];
      if (accepted.follow_up_of) {
        // Mid-turn injection: another Run is still working in this
        // conversation. Queue behind it — no separate card; the parent card
        // shows a "已收到你的补充" hint and the follow-up starts afterwards.
        setComposerState("READY");
        setLoading(false);
        registerFollowUpHint(accepted.follow_up_of, accepted.run_id, prompt);
        return;
      }
      if (accepted.status === "ACCEPTED" && accepted.created_at) {
        // Immediate accept: the Run is durably accepted; Agent reasoning runs
        // in the background. Subscribe to run events for meaningful activity
        // and keep the composer usable while the Run is being processed.
        setComposerState("READY");
        setLoading(false);
        setConcurrentRuns(previous => ({
          ...previous,
          [accepted.run_id]: {
            // A durable Run was accepted, but no business action has started
            // yet.  Do not turn an internal capability guess into user UI.
            title: "请求已收到",
            status: accepted.status,
            error: null
          }
        }));
        runControllersRef.current.set(accepted.run_id, controller);
        // Conversation-wide UserActivity SSE/polling is already active.
        // Legacy per-Run event subscription intentionally does not feed the
        // ordinary user-facing panel anymore.
        try {
          const completed = await waitForAgentRun(
            token,
            accepted.run_id,
            next => setConcurrentRuns(previous => ({
              ...previous,
              [accepted.run_id]: {
                ...(previous[accepted.run_id] || { title: "正在处理一项事情" }),
                status: next.status,
                error: next.error
              }
            })),
            controller.signal
          );
          if (completed.status === "FAILED") {
            // The run-keyed concurrent item is the terminal projection for a
            // 202-accepted Run. Rendering the generic alert as well creates a
            // second copy of the same failure; keep the stable run card as
            // the single terminal surface.
            setError(null);
            setConcurrentRuns(previous => ({
              ...previous,
              [accepted.run_id]: {
                ...(previous[accepted.run_id] || { title: "这项事情" }),
                status: completed.status,
                error: completed.error
              }
            }));
            runControllersRef.current.delete(accepted.run_id);
            // The parent failed terminally: queued follow-ups can now run.
            maybeAttachFollowUps([accepted.run_id]);
            return;
          }
          if (["WAITING_APPROVAL", "WAITING_HUMAN", "WAITING_USER", "PAUSED"].includes(completed.status)) {
            try {
              setMessages(await agentService.listMessages(
                token,
                conversation.conversation_id,
                controller.signal
              ));
            } catch {
              // Keep the waiting run visible; message polling can recover.
            }
            setRun(completed);
            runControllersRef.current.delete(accepted.run_id);
            return;
          }
          setMessages(await agentService.listMessages(
            token,
            conversation.conversation_id,
            controller.signal
          ));
          setConcurrentRuns(previous => {
            const next = { ...previous };
            delete next[accepted.run_id];
            return next;
          });
          setConcurrentActivities(previous => {
            const next = { ...previous };
            delete next[accepted.run_id];
            return next;
          });
          if (run?.run_id === accepted.run_id) setRun(null);
          // The parent finished: start any mid-turn follow-ups queued behind it.
          maybeAttachFollowUps([accepted.run_id]);
        } catch (caught) {
          if ((caught as DOMException)?.name !== "AbortError") {
            setError(friendlyClientError(caught));
          }
        }
        runControllersRef.current.delete(accepted.run_id);
        return;
      }
      if (executionIds.length) {
        const completedExecutions = await Promise.all(executionIds.map(executionId =>
          waitForExecution(
            token,
            executionId,
            snapshot => {
              setExecutions(previous => upsertExecution(previous, snapshot));
              if (executionIds.length === 1) {
                setExecution(snapshot);
                if (isTerminalExecution(snapshot.status)) setProjectionPending(true);
              }
            },
            undefined,
            controller.signal
          )
        ));
        const waitingForAction = completedExecutions.some(item =>
          ["WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(item.status)
        );
        if (!waitingForAction) setProjectionPending(true);
        setExecutions(completedExecutions);
        setExecution(completedExecutions[0] ?? null);
        if (waitingForAction) {
          setProjectionPending(false);
          try {
            setRun(await agentService.getRun(token, accepted.run_id, controller.signal));
          } catch {
            // Keep the activity card when the compatibility projection is unavailable.
          }
          return;
        }
        setProjectionPending(true);
        const refreshed = await refreshMessagesAfterExecution(
          token,
          conversation.conversation_id,
          previousAgentCount,
          executionIds,
          controller.signal
        );
        setMessages(refreshed.messages);
        setRun(null);
        if (refreshed.projected) {
          setExecution(null);
          setExecutions([]);
          setProjectionPending(false);
        } else {
          setExecution(completedExecutions[0] ?? null);
        }
        return;
      }
      if (accepted.error_code === "AMBIGUOUS_TARGET") {
        setMessages(await agentService.listMessages(
          token,
          conversation.conversation_id,
          controller.signal
        ));
        setRun(null);
        return;
      }
      const completed = await waitForAgentRun(
        token,
        accepted.run_id,
        setRun,
        controller.signal
      );
      if (completed.status === "FAILED") {
        throw new Error(completed.error || "任务执行失败");
      }
      if (["WAITING_APPROVAL", "WAITING_HUMAN", "WAITING_USER", "PAUSED"].includes(completed.status)) {
        try {
          setMessages(await agentService.listMessages(
            token,
            conversation.conversation_id,
            controller.signal
          ));
        } catch {
          // Keep the waiting run visible; message polling can recover.
        }
        setRun(completed);
        return;
      }
      setMessages(await agentService.listMessages(token, conversation.conversation_id));
      setRun(null);
    } catch (caught) {
      if ((caught as DOMException)?.name !== "AbortError") {
        setError(friendlyClientError(caught));
      }
    } finally {
      setLoading(false);
      setComposerState("READY");
    }
  };

  const semanticControlError = (caught: unknown): string =>
    semanticConfirmationErrorMessage(caught instanceof AgentApiError ? caught.status : undefined);

  const beginSemanticAction = (
    key: string,
    nextState: SemanticConfirmationViewState
  ): boolean => {
    if (semanticActionKeysRef.current.has(key)) return false;
    const current = semanticConfirmationStates[key];
    if (current === "CONFIRMING" || current === "CANCELLING") return false;
    semanticActionKeysRef.current.add(key);
    setSemanticConfirmationStates(previous => ({ ...previous, [key]: nextState }));
    setSemanticConfirmationErrors(previous => {
      if (!(key in previous)) return previous;
      const next = { ...previous };
      delete next[key];
      return next;
    });
    return true;
  };

  const handleSemanticConfirm = async (
    event: UserActivityEvent,
    payload: SemanticConfirmationPayload
  ) => {
    const key = semanticConfirmationKey(event, payload);
    if (!beginSemanticAction(key, "CONFIRMING")) return;
    const taskId = event.task_id?.trim();
    if (!token || !taskId) {
      semanticActionKeysRef.current.delete(key);
      setSemanticConfirmationStates(previous => ({ ...previous, [key]: "STALE" }));
      setSemanticConfirmationErrors(previous => ({
        ...previous,
        [key]: semanticConfirmationErrorMessage()
      }));
      return;
    }
    try {
      const response = await agentService.controlSemanticConfirmation(
        token,
        taskId,
        buildSemanticConfirmationControl(payload, "CONFIRM")
      );
      setSemanticConfirmationStates(previous => ({
        ...previous,
        [key]: response.resume_queued ? "WORKING" : "CONFIRMED"
      }));
    } catch (caught) {
      setSemanticConfirmationStates(previous => ({
        ...previous,
        [key]: caught instanceof AgentApiError && caught.status === 409 ? "STALE" : "WAITING_CONFIRMATION"
      }));
      setSemanticConfirmationErrors(previous => ({ ...previous, [key]: semanticControlError(caught) }));
    } finally {
      semanticActionKeysRef.current.delete(key);
    }
  };

  const handleSemanticCancel = async (
    event: UserActivityEvent,
    payload: SemanticConfirmationPayload
  ) => {
    const key = semanticConfirmationKey(event, payload);
    if (!beginSemanticAction(key, "CANCELLING")) return;
    const taskId = event.task_id?.trim();
    if (!token || !taskId) {
      semanticActionKeysRef.current.delete(key);
      setSemanticConfirmationStates(previous => ({ ...previous, [key]: "STALE" }));
      setSemanticConfirmationErrors(previous => ({
        ...previous,
        [key]: semanticConfirmationErrorMessage()
      }));
      return;
    }
    try {
      await agentService.controlSemanticConfirmation(
        token,
        taskId,
        buildSemanticConfirmationControl(payload, "CANCEL")
      );
      setSemanticConfirmationStates(previous => ({ ...previous, [key]: "CANCELLED" }));
    } catch (caught) {
      setSemanticConfirmationStates(previous => ({
        ...previous,
        [key]: caught instanceof AgentApiError && caught.status === 409 ? "STALE" : "WAITING_CONFIRMATION"
      }));
      setSemanticConfirmationErrors(previous => ({ ...previous, [key]: semanticControlError(caught) }));
    } finally {
      semanticActionKeysRef.current.delete(key);
    }
  };

  const handleSemanticModify = async (
    event: UserActivityEvent,
    payload: SemanticConfirmationPayload,
    modification: string
  ) => {
    const key = semanticConfirmationKey(event, payload);
    // Once the typed MODIFY CAS has superseded the frozen snapshot, retries
    // only re-submit the user's compilation input.  Replaying the old CAS
    // would correctly produce a 409 and would not help the user recover.
    if (semanticModifySupersededKeysRef.current.has(key)) {
      await send(modification);
      return;
    }
    if (!beginSemanticAction(key, "MODIFYING")) return;
    const taskId = event.task_id?.trim();
    if (!token || !taskId) {
      semanticActionKeysRef.current.delete(key);
      setSemanticConfirmationStates(previous => ({ ...previous, [key]: "STALE" }));
      setSemanticConfirmationErrors(previous => ({
        ...previous,
        [key]: semanticConfirmationErrorMessage()
      }));
      return;
    }
    try {
      const response = await agentService.controlSemanticConfirmation(
        token,
        taskId,
        buildSemanticConfirmationControl(payload, "MODIFY", modification)
      );
      if (!response.requires_new_compilation) return;
      semanticModifySupersededKeysRef.current.add(key);
      // The old snapshot is now permanently non-executable.  This is the
      // only normal-language submission in this flow, and it creates the new
      // semantic version through the existing compilation path.
      await send(modification);
    } catch (caught) {
      setSemanticConfirmationStates(previous => ({
        ...previous,
        [key]: caught instanceof AgentApiError && caught.status === 409 ? "STALE" : "MODIFYING"
      }));
      setSemanticConfirmationErrors(previous => ({ ...previous, [key]: semanticControlError(caught) }));
    } finally {
      semanticActionKeysRef.current.delete(key);
    }
  };

  const composeFollowUp = (prompt: string) => {
    setContent(prompt);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  };

  const decideApproval = async (decision: "APPROVE" | "REJECT") => {
    if (!token || !run?.approval || loading) return;
    setLoading(true);
    setError(null);
    const previousAgentCount = agentMessageCount(messages);
    let approvedExecution: Execution | null = null;
    let projected = true;
    try {
      const updated = await agentService.decideApproval(
        token,
        run.run_id,
        run.approval.approval_id,
        decision,
        run.approval.expected_run_version
      );
      setRun(updated);
      if (decision === "APPROVE") {
        const controller = new AbortController();
        runControllerRef.current = controller;
        if (execution) {
          approvedExecution = await waitForExecution(
            token,
            execution.execution_id,
            snapshot => {
              setExecution(snapshot);
              if (isTerminalExecution(snapshot.status)) setProjectionPending(true);
            },
            undefined,
            controller.signal
          );
          if (approvedExecution.status === "FAILED") {
            const failedStep = approvedExecution.steps?.find(step => step.error_message);
            throw new Error(failedStep?.error_message || "发布未完成");
          }
        } else {
          const completed = await waitForAgentRun(
            token,
            run.run_id,
            setRun,
            controller.signal
          );
          if (completed.status === "FAILED") {
            throw new Error(completed.error || "发布失败");
          }
        }
      }
      if (approvedExecution) {
        setProjectionPending(true);
        const refreshed = await refreshMessagesAfterExecution(
          token,
          run.conversation_id,
          previousAgentCount,
          approvedExecution ? [approvedExecution.execution_id] : []
        );
        projected = refreshed.projected;
        setMessages(refreshed.messages);
      } else {
        setMessages(await agentService.listMessages(token, run.conversation_id));
      }
      setRun(null);
      if (approvedExecution && !projected) {
        setExecution(approvedExecution);
        setProjectionPending(true);
      } else {
        setExecution(null);
        setExecutions([]);
        setProjectionPending(false);
      }
    } catch (caught) {
      setError(friendlyClientError(caught));
    } finally {
      setLoading(false);
    }
  };

  /**
   * Activity-native approval path.  The visible request and identifiers come
   * from the durable public Activity contract; Run polling here is only the
   * command transport needed by the existing approval endpoint, never a
   * source for a user-facing business completion claim.
   */
  const decideActivityApproval = async (
    activity: UserActivityEvent,
    decision: "APPROVE" | "REJECT"
  ) => {
    const approvalId = typeof activity.safe_payload.approval_id === "string"
      ? activity.safe_payload.approval_id.trim()
      : "";
    if (!token || !activity.run_id || !approvalId || loading) return;
    setLoading(true);
    setError(null);
    try {
      await agentService.decideApproval(
        token,
        activity.run_id,
        approvalId,
        decision,
        // The durable approval service is the authority; the current route
        // accepts this compatibility field but does not use a Run version.
        0
      );
      // The decision itself is now durable.  Keep the historical waiting
      // Activity visible, but prevent a second click from resubmitting it.
      setResolvedApprovalActivityIds(previous => ({
        ...previous,
        [activity.activity_id]: decision === "APPROVE" ? "APPROVED" : "REJECTED"
      }));
      if (decision === "APPROVE") {
        const controller = new AbortController();
        runControllerRef.current = controller;
        const settled = await waitForAgentRun(
          token,
          activity.run_id,
          () => undefined,
          controller.signal
        );
        if (settled.status === "FAILED") {
          throw new Error(settled.error || "审批后的操作没有完成");
        }
      }
      if (conversation) {
        setMessages(await agentService.listMessages(
          token,
          conversation.conversation_id
        ));
      }
      // Do not add a synthetic “approved” or “completed” Activity here.
      // The worker must still emit an observed Runtime result, which the
      // backend projector will persist and stream in its normal order.
    } catch (caught) {
      setError(friendlyClientError(caught));
    } finally {
      setLoading(false);
    }
  };

  const cancelRun = async () => {
    if (token && execution) {
      runControllerRef.current?.abort();
      setLoading(true);
      setError(null);
      try {
        const updated = await executionService.cancel(token, execution.execution_id);
        setExecution(previous => previous ? { ...previous, ...updated } : updated);
      } catch (caught) {
        setError(friendlyClientError(caught));
      } finally {
        setLoading(false);
      }
      return;
    }
    if (!token || !run || ["COMPLETED", "PARTIAL_SUCCESS", "FAILED", "CANCELLED"].includes(run.status)) return;
    // A compatibility Run without a canonical Execution cannot be cancelled
    // through the Runtime control API (no such endpoint exists).  Surface a
    // clear message instead of firing a request that always 404s.
    setError("该任务暂不支持直接取消，请等待执行完成或联系管理员。");
  };

  const interruptRun = async () => {
    if (token && execution) {
      try {
        const updated = await executionService.pause(token, execution.execution_id);
        setExecution(previous => previous ? { ...previous, ...updated } : updated);
      } catch (caught) {
        setError(friendlyClientError(caught));
      }
      return;
    }
    if (!token || !run || !["QUEUED", "RUNNING", "RETRYING", "WAITING_DEPENDENCY", "WAITING_LANE"].includes(run.status)) return;
    // No run-level pause endpoint exists; the canonical Execution control API
    // is the only supported pause path (handled above when an execution is
    // present).  Do not fire a request that always 404s.
    setError("该任务暂不支持直接暂停，请稍后在执行详情中操作。");
  };

  const continueRun = async (mode: "resume" | "retry") => {
    if (token && execution) {
      setLoading(true);
      setError(null);
      try {
        if (mode === "resume") {
          const updated = await executionService.resume(token, execution.execution_id);
          setExecution(previous => previous ? { ...previous, ...updated } : updated);
        } else {
          const failedStep = execution.steps?.find(step =>
            step.status === "FAILED" || step.status === "FAILED_RETRYABLE"
          );
          if (!failedStep) throw new Error("没有可重试的步骤");
          await executionService.retryStep(token, execution.execution_id, failedStep.step_id);
        }
        const controller = new AbortController();
        runControllerRef.current = controller;
        const completed = await waitForExecution(
          token,
          execution.execution_id,
          snapshot => {
            setExecution(snapshot);
            if (isTerminalExecution(snapshot.status)) setProjectionPending(true);
          },
          undefined,
          controller.signal
        );
        if (completed.status === "FAILED") {
          const failedStep = completed.steps?.find(step => step.error_message);
          throw new Error(failedStep?.error_message || "任务执行未完成");
        }
        if (["WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(completed.status)) return;
        if (conversation) {
          setProjectionPending(true);
          const refreshed = await refreshMessagesAfterExecution(
            token,
            conversation.conversation_id,
            agentMessageCount(messages),
            [completed.execution_id],
            controller.signal
          );
          setMessages(refreshed.messages);
          if (!refreshed.projected) {
            setExecution(completed);
            return;
          }
          setExecution(null);
          setExecutions([]);
          setProjectionPending(false);
          return;
        }
        setExecution(completed);
      } catch (caught) {
        if ((caught as DOMException)?.name !== "AbortError") {
          setError(friendlyClientError(caught));
        }
      } finally {
        setLoading(false);
      }
      return;
    }
    if (!token || !run) return;
    // No run-level resume/retry endpoint exists; a compatibility Run without
    // a canonical Execution cannot be resumed from the panel.  Fail with a
    // clear message instead of firing a request that always 404s.
    setError("该任务暂不支持在此恢复/重试，请在执行详情中操作。");
  };

  const projectedExecutionIds = new Set(
    messages.flatMap(message =>
      (message.parts || [])
        .filter(isExecutionResultPart)
        .map(part => part.execution.execution_id)
    )
  );
  const pendingExecutions = executions.filter(item =>
    !["COMPLETED", "FAILED", "CANCELLED"].includes(item.status)
      || !projectedExecutionIds.has(item.execution_id)
  );
  const fallbackExecution = execution
    && (!["COMPLETED", "FAILED", "CANCELLED"].includes(execution.status)
      || !projectedExecutionIds.has(execution.execution_id))
    ? execution
    : null;
  const visibleExecution = pendingExecutions[0] || fallbackExecution;
  const approvalInteraction = run
    ? projectAgentRunToUserFacingInteraction(run)
    : null;
  const approvalFallbackInteraction = projectPendingApprovalFallback(run, execution);
  const visibleApprovalInteraction = approvalInteraction || approvalFallbackInteraction;
  const hasDurableApprovalActivity = userActivities.some(event =>
    event.status === "WAITING_APPROVAL"
    && (!run?.run_id || event.run_id === run.run_id)
  );
  const latestMessage = messages[messages.length - 1];
  const latestAssistantHasProjection = latestMessage?.role === "assistant"
    && latestMessage.parts?.some(part => isExecutionResultPart(part) || isUserFacingInteractionPart(part));
  // LEGACY_FALLBACK: only while the Activity feed is unavailable.  New
  // approvals are rendered from NEEDS_APPROVAL Activity below.
  const runArtifactInteractions = run && visibleApprovalInteraction && !latestAssistantHasProjection
    && userActivities.length === 0
    ? projectAgentRunArtifactsToUserFacingInteractions(run)
    : [];
  const concurrentRunItems = Object.entries(concurrentRuns);
  const semanticConfirmationEvents = selectLatestSemanticConfirmationEvents(userActivities);
  const approvalResolutionByActivityId = { ...resolvedApprovalActivityIds };
  if (runsHydrated) {
    const activeApprovalRunIds = new Set([
      ...Object.keys(concurrentRuns),
      ...(run?.run_id ? [run.run_id] : [])
    ]);
    for (const activity of userActivities) {
      if (
        activity.activity_type === "NEEDS_APPROVAL"
        && activity.status === "WAITING_APPROVAL"
        && activity.run_id
        && !activeApprovalRunIds.has(activity.run_id)
      ) {
        approvalResolutionByActivityId[activity.activity_id] ||= "COMPLETED";
      }
    }
  }
  const regularUserActivities = userActivities.filter(event =>
    event.activity_type !== "NEEDS_SEMANTIC_CONFIRMATION"
  );
  // LEGACY_FALLBACK: historical Run/Execution/Step cards remain compiled for
  // rollback only.  The normal panel renders durable UserActivityEvent below.
  const showLiveActivity = false;
  const composerLocked = isComposerDisabled(
    composerState,
    Boolean(token && conversation)
  );

  if (!open) return null;

  return (
    <div className={styles.overlay} onMouseDown={event => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section
        ref={panelRef}
        className={styles.panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-title"
        onKeyDown={event => {
          if (event.key !== "Tab" || !panelRef.current) return;
          const focusable = Array.from(panelRef.current.querySelectorAll<HTMLElement>(
            "button:not(:disabled), textarea:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex='-1'])"
          ));
          if (!focusable.length) return;
          const first = focusable[0];
          const last = focusable[focusable.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }}
      >
        <header className={styles.header}>
          <div className={styles.identity}>
            <span className={styles.mark}><AgentIcon width={22} height={22} aria-hidden="true" /></span>
            <div>
              <h2 id="agent-title">GreenBook Agent</h2>
              <p>能检索、总结、创作，也能替你按时发布</p>
            </div>
          </div>
          <button className={styles.iconButton} type="button" onClick={onClose} aria-label="关闭 GreenBook Agent">
            <CloseIcon width={20} height={20} />
          </button>
        </header>

        {!token ? (
          <div className={styles.loginState}>
            <AgentIcon width={34} height={34} aria-hidden="true" />
            <strong>登录后，Agent才能代表你执行社区任务</strong>
            <span>它只会在你明确提出时创作或发布内容。</span>
            <Link to="/login">去登录</Link>
          </div>
        ) : (
          <>
            <div className={styles.thread} ref={scrollRef} aria-live="polite">
              <details className={styles.memorySettings}>
                <summary>Agent记忆 <small>当前为只读</small></summary>
                <p className={styles.memoryNote}>记忆写入功能暂未开放，偏好会在对话中被自动记录。</p>
                {memoryProfile ? (
                  <div className={styles.episodeSettings}>
                    <div className={styles.memoryControls}>
                      <label>
                        <input
                          type="checkbox"
                          checked={memoryProfile.episodic_enabled}
                          disabled
                        />
                        记住非敏感任务摘要
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={memoryProfile.semantic_enabled}
                          disabled
                        />
                        按语义召回相关任务
                      </label>
                    </div>
                    <p>
                      任务记忆保留 {memoryProfile.retention_days} 天；密钥、密码等敏感请求不会自动保存。
                    </p>
                    {memoryRecords.length > 0 ? (
                      <div className={styles.memoryRecords}>
                        <div className={styles.memoryRecordsTitle}>最近记住的内容</div>
                        <ul>
                          {memoryRecords.slice(0, 6).map(record => (
                            <li key={record.memory_id}>
                              <span className={styles.memoryType}>
                                {record.memory_type === "SEMANTIC" ? "偏好" : "任务"}
                              </span>
                              <span className={styles.memoryContent}>{record.content}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    ) : (
                      <p className={styles.memoryNote}>还没有记住任何内容，完成一些任务后这里会显示。</p>
                    )}
                  </div>
                ) : null}
              </details>
              {!messages.length && !loading ? (
                <div className={styles.welcome}>
                  <span className={styles.eyebrow}>社区任务Agent</span>
                  <h3>从一句话开始，后面的步骤交给我</h3>
                  <p>我会在需要时查找社区内容、生成草稿或安排发布，并用简单的状态告诉你进展。</p>
                  <p className={styles.naturalLanguageHint}>例如：“帮我找几篇关于 Agent 的帖子并总结共同方法”</p>
                </div>
              ) : null}

              {dedupeTerminalAgentMessages(messages).map((message, messageIndex, renderedMessages) => {
                const resultParts = message.parts?.filter(isExecutionResultPart) ?? [];
                const userFacingParts = message.parts?.filter(isUserFacingInteractionPart) ?? [];
                const userFacingInteractions = [
                  ...projectAgentMessageToUserFacingInteractions(resultParts),
                  ...userFacingParts.map(projectUserFacingInteractionPart)
                ];
                const clarificationParts = message.parts?.filter(isClarificationPart) ?? [];
                const hasStructuredUserFacingResult = resultParts.length > 0 || userFacingParts.length > 0;
                return (
                  <article
                    className={message.role === "user" ? styles.userMessage : styles.agentMessage}
                    key={message.message_id}
                  >
                  {message.role === "assistant" ? (
                    <span className={styles.messageAuthor}><AgentIcon width={16} height={16} aria-hidden="true" /> GreenBook Agent</span>
                  ) : null}
                  {message.role === "assistant" && hasStructuredUserFacingResult ? null : (
                    <div className={styles.messageText}>
                      {message.role === "assistant"
                        ? <AgentMarkdown content={userFacingMessage(message.content)} />
                        : message.content}
                    </div>
                  )}
                  {userFacingInteractions.length ? (
                    <AgentResultGroup
                      interactions={userFacingInteractions}
                      disabled={loading}
                    />
                  ) : null}
                  {clarificationParts.map(part => (
                    (() => {
                      if (isTargetClarificationResolved(renderedMessages, messageIndex)) {
                        return null;
                      }
                      const clarification = projectTargetClarification(part);
                      return (
                        <TargetClarificationPart
                          key={String(part.command.command_id ?? message.message_id)}
                          clarification={clarification.clarification}
                          disabled={loading}
                          onSelect={identity => {
                            const candidate = part.candidates.find(item => item.identity === identity);
                            if (!candidate) return;
                            const selectedTaskChanges = Array.isArray(part.command.task_changes)
                              ? part.command.task_changes.map(change => {
                                if (!change || typeof change !== "object" || Array.isArray(change)) {
                                  return change;
                                }
                                const delta = change as Record<string, unknown>;
                                const existingReference = delta.target_reference;
                                const targetReference = existingReference
                                  && typeof existingReference === "object"
                                  && !Array.isArray(existingReference)
                                  ? existingReference as Record<string, unknown>
                                  : {};
                                return {
                                  ...delta,
                                  target_reference: {
                                    ...targetReference,
                                    ...(candidate.resource_id
                                      ? {
                                        id: candidate.resource_id,
                                        resource_id: candidate.resource_id
                                      }
                                      : {}),
                                    ...(candidate.task_id ? { task_id: candidate.task_id } : {})
                                  }
                                };
                              })
                              : undefined;
                            const label = userFacingDisplayText(
                              candidate.label,
                              `目标 ${candidate.type.toLowerCase()}`
                            );
                            void send(`选择：${label}`, {
                              ...part.command,
                              target: {
                                ...(part.command.target ?? {}),
                                id: candidate.resource_id,
                                task_id: candidate.task_id,
                                resource_id: candidate.resource_id,
                                artifact_id: candidate.artifact_id,
                                execution_id: candidate.execution_id
                              },
                              ...(selectedTaskChanges ? { task_changes: selectedTaskChanges } : {})
                            });
                          }}
                        />
                      );
                    })()
                  ))}
                  </article>
                );
              })}

              {semanticConfirmationEvents.map(event => {
                const payload = projectSemanticConfirmation(event);
                if (!payload) return null;
                const key = semanticConfirmationKey(event, payload);
                const state = semanticConfirmationStates[key] || "WAITING_CONFIRMATION";
                return (
                  <SemanticConfirmationCard
                    key={key}
                    event={event}
                    state={state}
                    disabled={loading || state === "CONFIRMING" || state === "CANCELLING"}
                    error={semanticConfirmationErrors[key]}
                    onConfirm={handleSemanticConfirm}
                    onCancel={handleSemanticCancel}
                    onModify={handleSemanticModify}
                  />
                );
              })}

              <UserActivityCluster
                activities={regularUserActivities}
                disabled={loading}
                resolvedApprovalActivityIds={approvalResolutionByActivityId}
                onApprovalDecision={decideActivityApproval}
              />

              {understanding && showLiveActivity ? (
                <section className={styles.understandingCard} aria-live="polite">
                  <strong>{understanding.summary}</strong>
                  <ul className={styles.understandingList}>
                    {understanding.tasks.map((task, index) => (
                      <li key={index}>
                        {task.description || `任务 ${index + 1}`}
                        {task.requires_search ? "（需要先搜索）" : ""}
                        {task.publish_at ? (
                          <> · {formatBusinessDateTime(task.publish_at, getDisplayTimezone()) ?? task.publish_at} 发布</>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}

              {showLiveActivity && concurrentRunItems.length > 0 ? (
                <section className={styles.concurrentSummary} aria-live="polite">
                  <strong>
                    {concurrentRunItems.every(([, item]) =>
                      ["WAITING_USER", "WAITING_HUMAN", "WAITING_APPROVAL", "PAUSED"].includes(item.status)
                    )
                      ? `有 ${concurrentRunItems.length} 项内容等待你确认`
                      : `正在处理 ${concurrentRunItems.length} 项事情`}
                  </strong>
                  <div className={styles.concurrentList}>
                    {concurrentRunItems.map(([runId, item]) => {
                      // A queued mid-turn follow-up renders as a hint on its
                      // parent card, not as a second parallel card.
                      const queuedAsFollowUp = Object.values(followUpHints)
                        .some(hint => hint.followUpRunId === runId);
                      if (queuedAsFollowUp) return null;
                      const hint = followUpHints[runId];
                      const activity = concurrentActivities[runId] || { current: null, done: [] };
                      const failed = item.status === "FAILED";
                      return (
                        <div className={styles.concurrentItem} key={runId}>
                          <span className={failed ? styles.concurrentFailed : styles.concurrentMarker} aria-hidden="true">
                            {failed ? "!" : "•"}
                          </span>
                          <div>
                            <strong>{failed ? "这项事情没有完成" : runItemTitle(item)}</strong>
                            {activity.done.map((done, index) => (
                              <div className={styles.concurrentDone} key={`${runId}-${index}`}>
                                ✓ {done.title}
                                {typeof done.count === "number" ? ` ${done.count} 项` : ""}
                                {done.runAt ? (
                                  <> · {formatBusinessDateTime(done.runAt, getDisplayTimezone()) ?? done.runAt} 发布</>
                                ) : null}
                              </div>
                            ))}
                            {activity.current && !failed ? (
                              <div className={styles.concurrentCurrent}>• {activity.current}</div>
                            ) : null}
                            {hint ? (
                              <div className={styles.followUpHint}>
                                <span>已收到你的补充：{hint.message}</span>
                                <small>将在当前任务完成后处理</small>
                              </div>
                            ) : null}
                            {failed && item.error ? <small>这项请求暂时未完成，请稍后重试。</small> : null}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>
              ) : null}

              {showLiveActivity && pendingExecutions.length > 1 && !visibleApprovalInteraction ? (
                <AgentExecutionActivityGroup
                  executions={pendingExecutions}
                  projectionPending={projectionPending}
                />
              ) : null}

              {showLiveActivity && pendingExecutions.length <= 1 && visibleExecution && !visibleApprovalInteraction ? (
                <AgentExecutionActivityCard
                  execution={visibleExecution}
                  disabled={loading}
                  projectionPending={projectionPending}
                  handlers={{
                    onPause: () => void interruptRun(),
                    onResume: () => void continueRun("resume"),
                    onRetry: () => void continueRun("retry"),
                    onCancel: () => void cancelRun()
                  }}
                />
              ) : null}

              {runArtifactInteractions.length ? (
                <AgentResultGroup
                  interactions={runArtifactInteractions}
                  disabled={loading}
                />
              ) : null}

              {!hasDurableApprovalActivity && visibleApprovalInteraction?.kind === "APPROVAL_REQUEST" ? (
                <AgentApprovalCard
                  approval={visibleApprovalInteraction.approval}
                  disabled={loading}
                  onApprove={() => void decideApproval("APPROVE")}
                  onReject={() => void decideApproval("REJECT")}
                  onModify={() => {
                    composeFollowUp("继续修改当前内容，不要发布");
                    void decideApproval("REJECT");
                  }}
                />
              ) : null}

              {showLiveActivity && run && !visibleApprovalInteraction && !visibleExecution ? (
                <AgentRunActivityCard
                  run={run}
                  disabled={loading}
                  handlers={{
                    onPause: () => void interruptRun(),
                    onResume: () => void continueRun("resume"),
                    onRetry: () => void continueRun("retry"),
                    onCancel: () => void cancelRun()
                  }}
                />
              ) : null}

              {showLiveActivity && loading && !run && !visibleExecution && (
                runActivity.done.length > 0 || runActivity.current || pendingCapability
              ) ? (
                <div className={styles.thinking}>
                  {runActivity.done.map((item, index) => (
                    <div key={index} className={styles.activityDone}>
                      ✓ {item.title}{typeof item.count === "number" ? ` ${item.count} 篇` : ""}
                    </div>
                  ))}
                  {runActivity.current ? (
                    <div className={styles.activityCurrent}>
                      • {runActivity.current}
                    </div>
                  ) : null}
                  {!runActivity.current && runActivity.done.length === 0 && pendingCapability ? (
                    <div className={styles.activityCurrent}>
                      • {capabilityActiveLabel(pendingCapability) || "正在处理你的请求…"}
                    </div>
                  ) : null}
                </div>
              ) : null}
              {showLiveActivity && loading && !run && !visibleExecution && runActivity.done.length === 0 && !runActivity.current && !pendingCapability ? <div className={styles.thinking}>正在理解你的请求…</div> : null}
              {error ? <div className={styles.error} role="alert">{error}</div> : null}
            </div>

            <footer className={styles.composer}>
              <textarea
                ref={inputRef}
                value={content}
                onChange={event => setContent(event.target.value)}
                onKeyDown={event => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void send();
                  }
                }}
                placeholder={contextPostId ? "例如：总结这个帖子，提炼三个关键点" : "告诉我你想找什么、创作什么，或何时发布…"}
                name="agent-message"
                autoComplete="off"
                rows={2}
                disabled={composerLocked}
                aria-label="给 GreenBook Agent发送消息"
              />
              <div className={styles.composerMeta}>
                <span>Enter 发送 · Shift + Enter 换行</span>
                <button type="button" disabled={!content.trim() || composerLocked} onClick={() => void send()} aria-label="发送">
                  <SendIcon width={19} height={19} />
                </button>
              </div>
            </footer>
          </>
        )}
      </section>
    </div>
  );
};

const isClarificationPart = (
  part: AgentMessagePart
): part is AgentTargetClarificationPart => part.type === "target_clarification";

const isToolPart = (part: AgentMessagePart): part is AgentToolPart =>
  part.type !== "execution_result"
  && part.type !== "target_clarification"
  && "tool" in part;

const TargetClarificationPart = ({
  clarification,
  disabled,
  onSelect
}: {
  clarification: UserFacingTargetClarification;
  disabled?: boolean;
  onSelect: (identity: string) => void;
}) => (
  <section className={styles.clarificationCard} aria-label={clarification.question}>
    <div className={styles.clarificationHeading}>
      <SearchIcon width={18} height={18} aria-hidden="true" />
      <div>
        <strong>{clarification.question}</strong>
        <small>{clarification.description}</small>
      </div>
    </div>
    <div className={styles.clarificationOptions} role="list">
      {clarification.candidates.map((candidate, index) => (
        <button
          type="button"
          role="listitem"
          key={candidate.identity}
          disabled={disabled}
          onClick={() => onSelect(candidate.identity)}
        >
          <span className={styles.clarificationIndex}>{index + 1}</span>
          <span>
            <strong>{candidate.label}</strong>
            <small>{candidate.status}</small>
          </span>
        </button>
      ))}
    </div>
  </section>
);

const friendlyClientError = (caught: unknown): string => {
  const message = caught instanceof Error ? caught.message.toUpperCase() : "";
  if (/AUTH|TOKEN|401|403|UNAUTHORIZED|FORBIDDEN/.test(message)) {
    return "登录状态已失效，请重新登录后继续。";
  }
  if (/TIMEOUT|ABORT/.test(message)) return "任务等待超时，可以稍后重试。";
  if (/FETCH|NETWORK|CONNECT|UNAVAILABLE|502|503|504/.test(message)) {
    return "服务暂时无法连接，已有任务状态不会丢失。";
  }
    return "任务暂时无法继续。你可以稍后重试，或前往我的内容查看状态。";
};

/** LEGACY_FALLBACK: retained for a future developer-only inspector; not rendered by AgentPanel. */
const ToolPart = ({ part }: { part: AgentToolPart }) => {
  const result = part.result ?? {};
  const runAt = typeof result.run_at === "string" ? result.run_at : null;
  const timezone = getDisplayTimezone(typeof result.timezone === "string" ? result.timezone : undefined);
  const results = Array.isArray(result.results) ? result.results as Array<Record<string, unknown>> : [];
  return (
    <div className={styles.toolPart}>
      <span><CheckIcon width={14} height={14} aria-hidden="true" /></span>
      <strong>{part.label}</strong>
      {results.length ? (
        <ul>{results.slice(0, 5).map(item => <li key={String(item.id)}>{String(item.title || "未命名帖子")}</li>)}</ul>
      ) : null}
      {runAt ? (
        <small>计划时间 {formatBusinessDateTime(runAt, timezone) || runAt}</small>
      ) : null}
    </div>
  );
};

const ToolDetails = ({ parts }: { parts: AgentToolPart[] }) => {
  const [open, setOpen] = useState(false);
  return (
    <details
      className={styles.executionDetails}
      open={open}
      onToggle={event => setOpen(event.currentTarget.open)}
    >
      <summary>查看处理进度</summary>
      {open ? (
        <div className={styles.toolParts}>
          {parts.map(part => <ToolPart part={part} key={`${part.tool}-${part.label}`} />)}
        </div>
      ) : null}
    </details>
  );
};

export default AgentPanel;
