import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import AgentMarkdown from "@/components/content/AgentMarkdown";
import {
  AgentIcon,
  CheckIcon,
  ClockIcon,
  CloseIcon,
  SearchIcon,
  SendIcon
} from "@/components/icons/Icon";
import { agentService, waitForAgentRun } from "@/services/agentService";
import { executionService, waitForExecution } from "@/services/executionService";
import {
  runtimeExecutionButtonLabels,
  runtimeExecutionStatusLabel,
  runtimeStepLabel,
  runtimeStepStatusLabel
} from "@/services/runtimeExecutionLabels";
import type {
  AgentConversation,
  AgentEpisode,
  AgentMemory,
  AgentMemoryProfile,
  AgentMessage,
  AgentExecutionResultPart,
  AgentClarificationCandidate,
  AgentRun,
  AgentMessagePart,
  AgentTargetClarificationPart,
  AgentToolPart
} from "@/types/agent";
import type { Execution } from "@/types/execution";
import styles from "./AgentPanel.module.css";

type Props = {
  open: boolean;
  onClose: () => void;
  contextPostId?: string;
  surface?: "HOME" | "POST";
};

const SUGGESTIONS = [
  "明天上午八点发布一篇关于如何学好 Java 的帖子",
  "帮我找几篇时间管理的帖子并总结共同方法",
  "结合社区内容，创作一篇适合新人的学习复盘"
];

const ACTIVE_RUN_STATUSES = new Set([
  "QUEUED",
  "RUNNING",
  "RETRYING",
  "WAITING_DEPENDENCY",
  "WAITING_LANE",
  "WAITING_APPROVAL",
  "WAITING_HUMAN",
  "PAUSED"
]);

const agentMessageCount = (items: AgentMessage[]) =>
  items.filter(item => item.role === "assistant").length;

const upsertExecution = (items: Execution[], next: Execution): Execution[] => {
  const current = items.findIndex(item => item.execution_id === next.execution_id);
  if (current < 0) return [...items, next];
  return items.map((item, index) => index === current ? next : item);
};

const refreshMessagesAfterExecution = async (
  token: string,
  conversationId: string,
  previousAgentCount: number,
  signal?: AbortSignal
): Promise<{ messages: AgentMessage[]; projected: boolean }> => {
  let latest: AgentMessage[] = [];
  for (let attempt = 0; attempt < 20; attempt += 1) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    latest = await agentService.listMessages(token, conversationId, signal);
    if (agentMessageCount(latest) > previousAgentCount) {
      return { messages: latest, projected: true };
    }
    await new Promise(resolve => window.setTimeout(resolve, 200));
  }
  return { messages: latest, projected: false };
};

const toolLabel = (tool: string) => {
  if (tool.includes("search")) return "检索社区";
  if (tool.includes("summarize") || tool.includes("get_post")) return "阅读帖子";
  if (tool.includes("creator")) return "调用Creator Service";
  if (tool.includes("schedule")) return "安排定时发布";
  if (tool.includes("publish")) return "发布帖子";
  return "执行工具";
};

const AgentPanel = ({ open, onClose, contextPostId, surface = "HOME" }: Props) => {
  const { tokens, isLoading: authLoading } = useAuth();
  const token = tokens?.accessToken;
  const [conversation, setConversation] = useState<AgentConversation | null>(null);
  const [messages, setMessages] = useState<AgentMessage[]>([]);
  const [content, setContent] = useState("");
  const [run, setRun] = useState<AgentRun | null>(null);
  const [execution, setExecution] = useState<Execution | null>(null);
  const [executions, setExecutions] = useState<Execution[]>([]);
  const [memories, setMemories] = useState<AgentMemory[]>([]);
  const [episodes, setEpisodes] = useState<AgentEpisode[]>([]);
  const [memoryProfile, setMemoryProfile] = useState<AgentMemoryProfile | null>(null);
  const [memoryKey, setMemoryKey] = useState("");
  const [memoryValue, setMemoryValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const runControllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!open || !token || authLoading) return;
    const controller = new AbortController();
    const prepare = async () => {
      setLoading(true);
      setError(null);
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
        const [nextMessages, nextMemories, nextEpisodes, nextMemoryProfile] = await Promise.all([
          agentService.listMessages(token, current.conversation_id, controller.signal),
          agentService.listMemories(token, controller.signal),
          agentService.listEpisodes(token, controller.signal),
          agentService.getMemoryProfile(token, controller.signal)
        ]);
        setMessages(nextMessages);
        setMemories(nextMemories);
        setEpisodes(nextEpisodes);
        setMemoryProfile(nextMemoryProfile);

        // The execution card is transient UI state. Restore it from the
        // conversation's active run so reopening the panel does not leave
        // only the optimistic user message visible.
        const activeRun = (await agentService.listRuns(token, controller.signal))
          .filter(item => item.conversation_id === current.conversation_id)
          .filter(item => item.execution_id && ACTIVE_RUN_STATUSES.has(item.status))
          .sort((left, right) => right.updated_at.localeCompare(left.updated_at))[0];
        if (activeRun?.execution_id && !controller.signal.aborted) {
          const activeStatus = activeRun.status as string;
          if (activeStatus === "PAUSED" || activeStatus === "WAITING_APPROVAL" || activeStatus === "WAITING_HUMAN") {
            const snapshot = await executionService.get(token, activeRun.execution_id, controller.signal);
            setExecution(snapshot);
            setExecutions([snapshot]);
          } else {
            void waitForExecution(
              token,
              activeRun.execution_id,
              setExecution,
              undefined,
              controller.signal
            ).then(async completed => {
              if (controller.signal.aborted) return;
              setExecutions([completed]);
              if (completed.status === "COMPLETED" || completed.status === "CANCELLED") {
                const refreshed = await refreshMessagesAfterExecution(
                  token,
                  current.conversation_id,
                  agentMessageCount(nextMessages),
                  controller.signal
                );
                setMessages(refreshed.messages);
                if (refreshed.projected) setExecution(completed);
              }
            }).catch(caught => {
              if (!controller.signal.aborted && (caught as DOMException)?.name !== "AbortError") {
                setError(caught instanceof Error ? caught.message : "执行状态暂时无法更新");
              }
            });
          }
        }
      } catch (caught) {
        if (!controller.signal.aborted) {
          setError(caught instanceof Error ? caught.message : "Agent暂时无法连接");
        }
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    };
    void prepare();
    return () => controller.abort();
  }, [authLoading, contextPostId, open, surface, token]);

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
    };
  }, [onClose, open]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [execution?.events?.length, execution?.steps?.length, execution?.status, messages, run?.steps.length, run?.status]);

  const send = async (
    suggestion?: string,
    commandOverride?: Record<string, unknown>
  ) => {
    const prompt = (suggestion ?? content).trim();
    if (!prompt || !token || !conversation || loading) return;
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
    setRun(null);
    setExecution(null);
    setExecutions([]);
    setError(null);
    setLoading(true);
    runControllerRef.current?.abort();
    const controller = new AbortController();
    runControllerRef.current = controller;
    try {
      const accepted = await agentService.send(
        token,
        conversation.conversation_id,
        prompt,
        contextPostId,
        undefined,
        commandOverride
      );
      const executionIds = accepted.execution_ids?.length
        ? accepted.execution_ids
        : accepted.execution_id ? [accepted.execution_id] : [];
      if (executionIds.length) {
        const completedExecutions = await Promise.all(executionIds.map(executionId =>
          waitForExecution(
            token,
            executionId,
            snapshot => {
              setExecutions(previous => upsertExecution(previous, snapshot));
              if (executionIds.length === 1) setExecution(snapshot);
            },
            undefined,
            controller.signal
          )
        ));
        setExecutions(completedExecutions);
        setExecution(completedExecutions[0] ?? null);
        if (completedExecutions.some(item =>
          ["WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(item.status)
        )) {
          try {
            setRun(await agentService.getRun(token, accepted.run_id, controller.signal));
          } catch {
            // Keep the Runtime card when the compatibility projection is unavailable.
          }
          return;
        }
        const refreshed = await refreshMessagesAfterExecution(
          token,
          conversation.conversation_id,
          previousAgentCount,
          controller.signal
        );
        setMessages(refreshed.messages);
        setRun(null);
        if (refreshed.projected) {
          setExecution(completedExecutions[0] ?? null);
        } else {
          setExecution(completedExecutions[0] ?? null);
          setError("任务已完成，结果仍在同步，请稍后重试。");
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
        throw new Error(completed.error || "Agent任务执行失败");
      }
      if (["WAITING_APPROVAL", "PAUSED"].includes(completed.status)) {
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
          const completed = await waitForExecution(
            token,
            execution.execution_id,
            setExecution,
            undefined,
            controller.signal
          );
          if (completed.status === "FAILED") {
            const failedStep = completed.steps?.find(step => step.error_message);
            throw new Error(failedStep?.error_message || "Runtime publish failed");
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
      setMessages(await agentService.listMessages(token, run.conversation_id));
      setRun(null);
      setExecution(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "审批操作失败");
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
        setError(caught instanceof Error ? caught.message : "Runtime cancel failed");
      } finally {
        setLoading(false);
      }
      return;
    }
    if (!token || !run || ["COMPLETED", "FAILED", "CANCELLED"].includes(run.status)) return;
    runControllerRef.current?.abort();
    setLoading(true);
    setError(null);
    try {
      setRun(await agentService.cancelRun(token, run.run_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "取消任务失败");
    } finally {
      setLoading(false);
    }
  };

  const interruptRun = async () => {
    if (token && execution) {
      try {
        const updated = await executionService.pause(token, execution.execution_id);
        setExecution(previous => previous ? { ...previous, ...updated } : updated);
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : "Runtime pause failed");
      }
      return;
    }
    if (!token || !run || !["QUEUED", "RUNNING", "RETRYING", "WAITING_DEPENDENCY", "WAITING_LANE"].includes(run.status)) return;
    runControllerRef.current?.abort();
    setError(null);
    try {
      setRun(await agentService.interruptRun(token, run.run_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "暂停任务失败");
    } finally {
      setLoading(false);
    }
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
          if (!failedStep) throw new Error("No retryable Runtime step");
          await executionService.retryStep(token, execution.execution_id, failedStep.step_id);
        }
        const controller = new AbortController();
        runControllerRef.current = controller;
        const completed = await waitForExecution(
          token,
          execution.execution_id,
          setExecution,
          undefined,
          controller.signal
        );
        if (completed.status === "FAILED") {
          const failedStep = completed.steps?.find(step => step.error_message);
          throw new Error(failedStep?.error_message || "Runtime execution failed");
        }
        if (["WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(completed.status)) return;
        if (conversation) {
          const refreshed = await refreshMessagesAfterExecution(
            token,
            conversation.conversation_id,
            agentMessageCount(messages),
            controller.signal
          );
          setMessages(refreshed.messages);
          if (!refreshed.projected) {
            setExecution(completed);
            setError("任务已完成，结果仍在同步，请稍后重试。");
            return;
          }
        }
        setExecution(completed);
      } catch (caught) {
        if ((caught as DOMException)?.name !== "AbortError") {
          setError(caught instanceof Error ? caught.message : "Runtime resume failed");
        }
      } finally {
        setLoading(false);
      }
      return;
    }
    if (!token || !run) return;
    setLoading(true);
    setError(null);
    try {
      const queued = mode === "resume"
        ? await agentService.resumeRun(token, run.run_id)
        : await agentService.retryRun(token, run.run_id);
      setRun(queued);
      const controller = new AbortController();
      runControllerRef.current = controller;
      const completed = await waitForAgentRun(token, run.run_id, setRun, controller.signal);
      if (completed.status === "FAILED") throw new Error(completed.error || "任务执行失败");
      if (["WAITING_APPROVAL", "PAUSED"].includes(completed.status)) return;
      setMessages(await agentService.listMessages(token, run.conversation_id));
      setRun(null);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "任务恢复失败");
    } finally {
      setLoading(false);
    }
  };

  const saveMemory = async () => {
    if (!token || !memoryKey.trim() || !memoryValue.trim()) return;
    try {
      const saved = await agentService.saveMemory(
        token,
        memoryKey.trim(),
        memoryValue.trim()
      );
      setMemories(previous => [
        saved,
        ...previous.filter(item => item.memory_id !== saved.memory_id && item.key !== saved.key)
      ]);
      setMemoryKey("");
      setMemoryValue("");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "偏好保存失败");
    }
  };

  const deleteMemory = async (memoryId: string) => {
    if (!token) return;
    if (!window.confirm("删除这条Agent偏好？删除后无法恢复。")) return;
    try {
      await agentService.deleteMemory(token, memoryId);
      setMemories(previous => previous.filter(item => item.memory_id !== memoryId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "偏好删除失败");
    }
  };

  const updateMemoryProfile = async (
    field: "episodic_enabled" | "semantic_enabled",
    enabled: boolean
  ) => {
    if (!token || !memoryProfile) return;
    const next = {
      episodic_enabled: memoryProfile.episodic_enabled,
      semantic_enabled: memoryProfile.semantic_enabled,
      [field]: enabled
    };
    if (!next.episodic_enabled) next.semantic_enabled = false;
    if (field === "semantic_enabled" && enabled) next.episodic_enabled = true;
    try {
      setMemoryProfile(await agentService.updateMemoryProfile(token, next));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "记忆设置更新失败");
    }
  };

  const deleteEpisode = async (episodeId: string) => {
    if (!token || !window.confirm("删除这条任务记忆？删除后无法恢复。")) return;
    try {
      await agentService.deleteEpisode(token, episodeId);
      setEpisodes(previous => previous.filter(item => item.episode_id !== episodeId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "任务记忆删除失败");
    }
  };

  const clearEpisodes = async () => {
    if (!token || !episodes.length) return;
    if (!window.confirm(
      `清空全部任务记忆？当前列表显示 ${episodes.length} 条，删除后无法恢复。`
    )) return;
    try {
      await agentService.clearEpisodes(token);
      setEpisodes([]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "任务记忆清空失败");
    }
  };

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
                <summary>Agent记忆 <small>可查看、关闭和删除</small></summary>
                {memories.length ? (
                  <div className={styles.memoryList}>
                    {memories.map(memory => (
                      <div key={memory.memory_id}>
                        <span><strong>{memory.key}</strong>{memory.value}</span>
                        <button type="button" onClick={() => void deleteMemory(memory.memory_id)}>删除</button>
                      </div>
                    ))}
                  </div>
                ) : <p>尚未保存偏好；偏好只会在你主动添加时保存。</p>}
                <form className={styles.memoryForm} onSubmit={event => {
                  event.preventDefault();
                  void saveMemory();
                }}>
                  <input
                    aria-label="偏好名称"
                    name="agent-memory-key"
                    autoComplete="off"
                    placeholder="例如：写作语气…"
                    value={memoryKey}
                    onChange={event => setMemoryKey(event.target.value)}
                  />
                  <input
                    aria-label="偏好内容"
                    name="agent-memory-value"
                    autoComplete="off"
                    placeholder="例如：简洁、少用术语…"
                    value={memoryValue}
                    onChange={event => setMemoryValue(event.target.value)}
                  />
                  <button
                    type="submit"
                    disabled={!memoryKey.trim() || !memoryValue.trim()}
                  >
                    保存
                  </button>
                </form>
                {memoryProfile ? (
                  <div className={styles.episodeSettings}>
                    <div className={styles.memoryControls}>
                      <label>
                        <input
                          type="checkbox"
                          checked={memoryProfile.episodic_enabled}
                          onChange={event => void updateMemoryProfile(
                            "episodic_enabled",
                            event.target.checked
                          )}
                        />
                        记住非敏感任务摘要
                      </label>
                      <label>
                        <input
                          type="checkbox"
                          checked={memoryProfile.semantic_enabled}
                          disabled={!memoryProfile.episodic_enabled}
                          onChange={event => void updateMemoryProfile(
                            "semantic_enabled",
                            event.target.checked
                          )}
                        />
                        按语义召回相关任务
                      </label>
                    </div>
                    <p>
                      任务记忆保留 {memoryProfile.retention_days} 天；密钥、密码等敏感请求不会自动保存。
                    </p>
                    {episodes.length ? (
                      <>
                        <div className={styles.episodeList}>
                          {episodes.map(episode => (
                            <div key={episode.episode_id}>
                              <span>
                                <strong>{episode.goal}</strong>
                                <small>
                                  {new Date(episode.occurred_at).toLocaleString()}
                                  {episode.recall_count ? ` · 已召回 ${episode.recall_count} 次` : ""}
                                </small>
                              </span>
                              <button
                                type="button"
                                onClick={() => void deleteEpisode(episode.episode_id)}
                              >
                                删除
                              </button>
                            </div>
                          ))}
                        </div>
                        <button
                          className={styles.clearMemoryButton}
                          type="button"
                          onClick={() => void clearEpisodes()}
                        >
                          清空任务记忆
                        </button>
                      </>
                    ) : <p>完成非敏感任务后，相关摘要会显示在这里。</p>}
                  </div>
                ) : null}
              </details>
              {!messages.length && !loading ? (
                <div className={styles.welcome}>
                  <span className={styles.eyebrow}>社区任务Agent</span>
                  <h3>从一句话开始，后面的步骤交给我</h3>
                  <p>我会展示执行进度和真实结果。涉及创作时调用 Creator Service，帖子数据与发布权限仍由 GreenBook 负责。</p>
                  <div className={styles.suggestions}>
                    {SUGGESTIONS.map(item => (
                      <button key={item} type="button" onClick={() => void send(item)}>
                        <span>{item}</span>
                        <SendIcon width={16} height={16} aria-hidden="true" />
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}

              {messages.map(message => {
                const resultParts = message.parts?.filter(isExecutionResultPart) ?? [];
                const clarificationParts = message.parts?.filter(isClarificationPart) ?? [];
                return (
                  <article
                    className={message.role === "user" ? styles.userMessage : styles.agentMessage}
                    key={message.message_id}
                  >
                  {message.role === "assistant" ? (
                    <span className={styles.messageAuthor}><AgentIcon width={16} height={16} aria-hidden="true" /> GreenBook Agent</span>
                  ) : null}
                  <div className={styles.messageText}>
                    {message.role === "assistant"
                      ? <AgentMarkdown content={
                          resultParts.length
                            ? structuredResultLead(resultParts)
                            : message.content
                        } />
                      : message.content}
                  </div>
                  {resultParts.length ? (
                    <ExecutionResultGroup
                      parts={resultParts}
                      onCompose={composeFollowUp}
                    />
                  ) : null}
                  {clarificationParts.map(part => (
                    <TargetClarificationPart
                      key={String(part.command.command_id ?? message.message_id)}
                      part={part}
                      disabled={loading}
                      onSelect={(candidate, command) => {
                        const label = candidate.label || `目标 ${candidate.identity}`;
                        void send(`选择：${label}`, command);
                      }}
                    />
                  ))}
                  {message.parts?.some(isToolPart) ? (
                    <details className={styles.executionDetails}>
                      <summary>查看本次执行记录</summary>
                      <div className={styles.toolParts}>
                        {message.parts.filter(isToolPart).map(part => (
                          <ToolPart part={part} key={`${part.tool}-${part.label}`} />
                        ))}
                      </div>
                    </details>
                  ) : null}
                  </article>
                );
              })}

              {executions.length > 1 ? (
                <MultiExecutionCard executions={executions} />
              ) : null}

              {execution && executions.length <= 1 ? (
                <article className={styles.runCard} data-execution-id={execution.execution_id}>
                  <div className={styles.runHeading}>
                    <span
                      className={execution.status === "COMPLETED"
                        ? styles.completedDot
                        : execution.control_state === "CANCELLED"
                          ? styles.stopped
                          : styles.pulse}
                      aria-hidden="true"
                    />
                    <strong>
                      Runtime 执行 · {execution.control_state === "PAUSING"
                        ? "正在暂停"
                        : execution.control_state === "RESUMING"
                          ? "正在继续"
                          : runtimeExecutionStatusLabel(execution.status)}
                    </strong>
                    {(execution.control_state === "RUNNING"
                      && ["PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_APPROVAL"].includes(execution.status)) ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void interruptRun()}>
                        {runtimeExecutionButtonLabels.pause}
                      </button>
                    ) : null}
                    {execution.control_state === "PAUSED" ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void continueRun("resume")}>
                        {runtimeExecutionButtonLabels.resume}
                      </button>
                    ) : null}
                    {execution.status === "FAILED" ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void continueRun("retry")}>
                        {runtimeExecutionButtonLabels.retry}
                      </button>
                    ) : null}
                    {execution.control_state !== "CANCELLED"
                      && !["COMPLETED", "FAILED", "CANCELLED"].includes(execution.status) ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void cancelRun()}>
                        {runtimeExecutionButtonLabels.cancel}
                      </button>
                    ) : null}
                  </div>
                  <p className={styles.executionSummary} aria-live="polite">
                    {executionProgressSummary(execution)}
                  </p>
                  {execution.status === "FAILED" ? (
                    <FailureNotice execution={execution} />
                  ) : null}
                  <details className={styles.executionProcess}>
                    <summary>
                      <span>查看执行过程</span>
                      <small>{execution.completed_steps}/{execution.total_steps} 步</small>
                    </summary>
                    <div className={styles.steps}>
                      {execution.steps?.map(step => (
                        <div className={styles.step} key={step.step_execution_id || step.step_id}>
                          <span className={styles.stepIcon} aria-hidden="true">
                            {step.status === "COMPLETED"
                              ? <CheckIcon width={15} height={15} />
                              : step.capability?.includes("schedule")
                                ? <ClockIcon width={15} height={15} />
                                : <SearchIcon width={15} height={15} />}
                          </span>
                          <span>{runtimeStepLabel(step.capability || step.step_id)}</span>
                          <small>{runtimeStepStatusLabel(step.status)}</small>
                        </div>
                      ))}
                    </div>
                    <small className={styles.executionRecordHint}>
                      完整任务记录可在任务中心查看。
                    </small>
                  </details>
                </article>
              ) : null}

              {run ? (
                <article className={styles.runCard}>
                  <div className={styles.runHeading}>
                    <span className={run.status === "CANCELLED" ? styles.stopped : styles.pulse} aria-hidden="true" />
                    <strong>{run.summary || "正在理解并执行任务"}</strong>
                    {!run.approval && ["QUEUED", "RUNNING", "RETRYING", "WAITING_DEPENDENCY", "WAITING_LANE"].includes(run.status) ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void interruptRun()}>
                        暂停
                      </button>
                    ) : null}
                    {run.status === "PAUSED" ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void continueRun("resume")}>
                        继续
                      </button>
                    ) : null}
                    {run.status === "FAILED" ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void continueRun("retry")}>
                        重试
                      </button>
                    ) : null}
                    {!run.approval && !["COMPLETED", "FAILED", "CANCELLED", "PAUSED"].includes(run.status) ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void cancelRun()}>
                        停止
                      </button>
                    ) : null}
                  </div>
                  <div className={styles.steps}>
                    {run.steps.map(step => (
                      <div className={styles.step} key={step.step_id}>
                        <span className={styles.stepIcon} aria-hidden="true">
                          {step.status === "COMPLETED"
                            ? <CheckIcon width={15} height={15} />
                            : step.tool_name?.includes("schedule")
                              ? <ClockIcon width={15} height={15} />
                              : <SearchIcon width={15} height={15} />}
                        </span>
                        <span>
                          {step.label}
                          {step.agent_name ? <small> · {step.agent_name}</small> : null}
                        </span>
                        <small>
                          {step.status === "COMPLETED"
                            ? "完成"
                            : step.status === "FAILED"
                              ? "失败"
                              : step.status === "CANCELLED"
                                ? "已停止"
                                : step.status === "WAITING_DEPENDENCY"
                                  ? "等待依赖"
                                  : "执行中"}
                        </small>
                      </div>
                    ))}
                  </div>
                  {run.approval ? (
                    <div className={styles.approval}>
                      <div>
                        <strong>
                          {run.approval.action.includes("delete")
                            ? "删除前需要你确认"
                            : "发布前需要你确认"}
                        </strong>
                        <p>{run.approval.description}</p>
                        {typeof run.approval.preview.draft_id === "string"
                          ? <small>草稿号 {run.approval.preview.draft_id}</small>
                          : null}
                        {Array.isArray(run.approval.preview.items)
                          ? <small>将安排 {run.approval.preview.items.length} 篇草稿</small>
                          : null}
                        {Array.isArray(run.approval.preview.post_ids)
                          ? <small>将软删除你的 {run.approval.preview.post_ids.length} 篇帖子</small>
                          : null}
                      </div>
                      <div className={styles.approvalActions}>
                        <button type="button" onClick={() => void decideApproval("REJECT")}>
                          取消
                        </button>
                        <button type="button" onClick={() => void decideApproval("APPROVE")}>
                          {run.approval.action.includes("delete")
                            ? "确认删除"
                            : "确认发布"}
                        </button>
                      </div>
                    </div>
                  ) : null}
                  <details className={styles.runMeta}>
                    <summary>运行详情</summary>
                    <span>追踪号 {run.trace_id.slice(0, 8)}</span>
                    <span>
                      路径 {{
                        ROUTING: "路由中",
                        DIRECT: "直接回答",
                        TOOL: "单工具",
                        CREATOR: "AI 创作",
                        ORCHESTRATED: "完整编排"
                      }[run.execution_path]}
                    </span>
                    <span>通道 {run.workload_lane === "WRITE" ? "串行写入" : run.workload_lane === "READ" ? "并发只读" : "路由中"}</span>
                    <span>模型 {run.budget.model_calls}/{run.budget.max_model_calls}</span>
                    <span>工具 {run.budget.tool_calls}/{run.budget.max_tool_calls}</span>
                    <span>模型耗时 {(run.timing.model_ms / 1000).toFixed(1)}s</span>
                    <span>工具耗时 {(run.timing.tool_ms / 1000).toFixed(1)}s</span>
                  </details>
                </article>
              ) : null}

              {loading && !run ? <div className={styles.thinking}>正在建立任务…</div> : null}
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
                disabled={loading}
                aria-label="给 GreenBook Agent发送消息"
              />
              <div className={styles.composerMeta}>
                <span>Enter 发送 · Shift + Enter 换行</span>
                <button type="button" disabled={!content.trim() || loading} onClick={() => void send()} aria-label="发送">
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

const isExecutionResultPart = (
  part: AgentMessagePart
): part is AgentExecutionResultPart => part.type === "execution_result";

const isClarificationPart = (
  part: AgentMessagePart
): part is AgentTargetClarificationPart => part.type === "target_clarification";

const isToolPart = (part: AgentMessagePart): part is AgentToolPart =>
  part.type !== "execution_result"
  && part.type !== "target_clarification"
  && "tool" in part;

const TargetClarificationPart = ({
  part,
  disabled,
  onSelect
}: {
  part: AgentTargetClarificationPart;
  disabled?: boolean;
  onSelect: (
    candidate: AgentClarificationCandidate,
    command: Record<string, unknown>
  ) => void;
}) => (
  <section className={styles.clarificationCard} aria-label="请选择要操作的目标">
    <div className={styles.clarificationHeading}>
      <SearchIcon width={18} height={18} aria-hidden="true" />
      <div>
        <strong>请选择要操作的目标</strong>
        <small>选择后会继续原来的命令，不会创建重复任务。</small>
      </div>
    </div>
    <div className={styles.clarificationOptions} role="list">
      {part.candidates.map((candidate, index) => (
        <button
          type="button"
          role="listitem"
          key={candidate.identity}
          disabled={disabled}
          onClick={() => onSelect(candidate, {
            ...part.command,
            target: {
              ...(part.command.target ?? {}),
              task_id: candidate.task_id,
              resource_id: candidate.resource_id,
              artifact_id: candidate.artifact_id,
              execution_id: candidate.execution_id
            }
          })}
        >
          <span className={styles.clarificationIndex}>{index + 1}</span>
          <span>
            <strong>{candidate.label || `未命名${candidate.type.toLowerCase()}`}</strong>
            <small>{friendlyResultStatus(candidate.status || candidate.type)}</small>
          </span>
        </button>
      ))}
    </div>
  </section>
);

const ExecutionResultGroup = ({
  parts,
  onCompose
}: {
  parts: AgentExecutionResultPart[];
  onCompose: (prompt: string) => void;
}) => {
  if (parts.length === 1) {
    return <ExecutionResultPart part={parts[0]} onCompose={onCompose} />;
  }
  const completed = parts.filter(part => part.execution.status === "COMPLETED").length;
  const failed = parts.filter(part => part.execution.status === "FAILED").length;
  return (
    <section className={styles.resultGroup} aria-label="本次多任务结果">
      <header className={styles.resultGroupHeading}>
        <div>
          <strong>本次任务</strong>
          <small>
            {completed} 项完成
            {failed ? ` · ${failed} 项需要处理` : ""}
          </small>
        </div>
        <span>{parts.length} 项</span>
      </header>
      <div className={styles.resultGroupItems}>
        {parts.map((part, index) => (
          <ExecutionResultPart
            part={part}
            onCompose={onCompose}
            position={index + 1}
            key={part.execution.execution_id}
          />
        ))}
      </div>
    </section>
  );
};

const ExecutionResultPart = ({
  part,
  onCompose,
  position
}: {
  part: AgentExecutionResultPart;
  onCompose: (prompt: string) => void;
  position?: number;
}) => {
  const draft = part.artifacts.find(artifact =>
    artifact.resource_type === "DRAFT" || ["DRAFT", "POST_DRAFT", "CONTENT_DRAFT"].includes(artifact.type)
  );
  const scheduleArtifact = part.artifacts.find(artifact =>
    artifact.resource_type === "SCHEDULE" || ["SCHEDULE", "PUBLICATION_SCHEDULE"].includes(artifact.type)
  );
  const schedule = part.schedule ?? {};
  const runAt = scheduleArtifact?.run_at
    ?? scheduleArtifact?.publish_time
    ?? (typeof schedule.run_at === "string" ? schedule.run_at : null);
  const timezone = scheduleArtifact?.timezone
    ?? (typeof schedule.timezone === "string" ? schedule.timezone : "Asia/Shanghai");
  const scheduleId = scheduleArtifact?.resource_id
    ?? (typeof schedule.schedule_id === "string" ? schedule.schedule_id : null);
  const status = scheduleArtifact?.status
    ?? (typeof schedule.status === "string" ? schedule.status : part.execution.status);
  const formattedRunAt = formatResultTime(runAt, timezone);
  const failed = part.execution.status === "FAILED";
  const cancelled = part.execution.status === "CANCELLED";
  const subject = draft?.title || part.execution.summary || "这项任务";
  const resultTitle = failed
    ? "任务未完成"
    : cancelled
      ? "任务已取消"
      : draft && (runAt || scheduleId)
        ? "内容已创建并安排发布"
        : draft
          ? "内容创作已完成"
          : "任务已完成";

  return (
    <section
      className={`${styles.resultCard} ${failed ? styles.resultCardFailed : ""}`}
      aria-label={failed ? "任务失败结果" : "任务完成结果"}
    >
      <div className={styles.resultHeading}>
        <span className={failed ? styles.resultFailure : styles.resultCheck} aria-hidden="true">
          {failed ? <CloseIcon width={16} height={16} /> : <CheckIcon width={16} height={16} />}
        </span>
        <div>
          <strong>{position ? `${position}. ${resultTitle}` : resultTitle}</strong>
          <small>{friendlyResultStatus(status)}</small>
        </div>
      </div>

      {!failed && part.execution.summary ? (
        <p className={styles.resultSummary}>{part.execution.summary}</p>
      ) : null}

      {draft ? (
        <div className={styles.resultContent}>
          <span>草稿</span>
          <strong>{draft.title || "未命名草稿"}</strong>
          {draft.summary || draft.content ? <p>{draft.summary || draft.content}</p> : null}
          {draft.resource_id ? <small>草稿 ID：{draft.resource_id}</small> : null}
        </div>
      ) : null}

      {runAt || scheduleId ? (
        <div className={styles.scheduleSummary}>
          <ClockIcon width={16} height={16} aria-hidden="true" />
          <div>
            {formattedRunAt ? <strong>{formattedRunAt}</strong> : null}
            <small>
              {status || "等待发布"}
              {scheduleId ? ` · 定时任务 ${scheduleId}` : ""}
            </small>
          </div>
        </div>
      ) : null}

      <div className={styles.resultActions} aria-label="结果操作">
        {draft?.resource_id ? (
          <Link to={`/create/manual?draftId=${encodeURIComponent(draft.resource_id)}`}>
            查看文章
          </Link>
        ) : null}
        {draft ? (
          <button type="button" onClick={() => onCompose(`修改「${subject}」的内容`)}>
            修改内容
          </button>
        ) : null}
        {scheduleId ? (
          <button type="button" onClick={() => onCompose(`调整「${subject}」的发布时间`)}>
            调整发布时间
          </button>
        ) : null}
        {scheduleId ? (
          <button type="button" onClick={() => onCompose(`取消「${subject}」的发布计划`)}>
            取消发布
          </button>
        ) : null}
        <Link to="/tasks">查看任务详情</Link>
      </div>

      <details className={styles.resultExecution}>
        <summary>查看执行详情</summary>
        {part.execution.steps?.length ? (
          <ol>
            {part.execution.steps.map(step => (
              <li key={step.step_id || step.label}>
                <span>{runtimeStepLabel(step.label || step.step_id || "")}</span>
                <small>{runtimeStepStatusLabel(step.status || "PENDING")}</small>
              </li>
            ))}
          </ol>
        ) : null}
      </details>
    </section>
  );
};

const MultiExecutionCard = ({ executions }: { executions: Execution[] }) => {
  const completed = executions.filter(item => item.status === "COMPLETED").length;
  const failed = executions.filter(item => item.status === "FAILED").length;
  return (
    <article className={styles.multiExecutionCard} aria-label="多任务执行状态">
      <header className={styles.multiExecutionHeading}>
        <div>
          <strong>正在处理本次任务</strong>
          <small aria-live="polite">
            {completed}/{executions.length} 项完成
            {failed ? ` · ${failed} 项需要处理` : ""}
          </small>
        </div>
        <span>{Math.round((completed / executions.length) * 100)}%</span>
      </header>
      <ol className={styles.multiExecutionList}>
        {executions.map((item, index) => (
          <li key={item.execution_id}>
            <span className={styles.multiExecutionIndex}>{index + 1}</span>
            <div>
              <strong>{item.task_id ? `任务 ${index + 1}` : "子任务"}</strong>
              <small>{executionProgressSummary(item)}</small>
            </div>
            <span className={styles.multiExecutionStatus}>
              {runtimeExecutionStatusLabel(item.status)}
            </span>
          </li>
        ))}
      </ol>
      <details className={styles.executionProcess}>
        <summary>查看全部执行过程</summary>
        {executions.map((item, index) => (
          <div className={styles.multiExecutionSteps} key={item.execution_id}>
            <strong>任务 {index + 1}</strong>
            {item.steps?.map(step => (
              <span key={step.step_execution_id || step.step_id}>
                {runtimeStepLabel(step.capability || step.step_id)}
                <small>{runtimeStepStatusLabel(step.status)}</small>
              </span>
            ))}
          </div>
        ))}
      </details>
    </article>
  );
};

const FailureNotice = ({ execution }: { execution: Execution }) => {
  const failure = friendlyExecutionFailure(execution);
  return (
    <section className={styles.failureNotice} role="alert">
      <strong>{failure.title}</strong>
      <p>{failure.reason}</p>
      <small>{failure.recovery}</small>
    </section>
  );
};

const executionProgressSummary = (execution: Execution): string => {
  if (execution.status === "COMPLETED") return "任务已完成，结果已保存。";
  if (execution.status === "FAILED") return "任务没有完成，已有结果不会丢失。";
  if (execution.status === "CANCELLED" || execution.control_state === "CANCELLED") {
    return "任务已取消，不会继续执行后续步骤。";
  }
  if (execution.control_state === "PAUSED" || execution.status === "PAUSED") {
    return "任务已暂停，可以稍后继续。";
  }
  const step = runtimeStepLabel(execution.current_step || "");
  return step
    ? `正在进行：${step}`
    : `已完成 ${execution.completed_steps}/${execution.total_steps} 个步骤`;
};

const structuredResultLead = (parts: AgentExecutionResultPart[]): string => {
  if (parts.length > 1) {
    const completed = parts.filter(part => part.execution.status === "COMPLETED").length;
    const failed = parts.filter(part => part.execution.status === "FAILED").length;
    if (failed) {
      return `我已经处理了本次的 ${parts.length} 项任务：${completed} 项完成，${failed} 项需要你处理。`;
    }
    return `我已经处理完本次的 ${parts.length} 项任务，结果都整理在下面。`;
  }
  const part = parts[0];
  if (part.execution.status === "FAILED") {
    return "这项任务没有完成。我保留了已有结果，并整理了可以继续处理的方式。";
  }
  if (part.execution.status === "CANCELLED") {
    return "我已经停止这项任务，后续步骤不会继续执行。";
  }
  return "我已经完成这项任务，生成的内容和可继续操作的选项都在下面。";
};

const friendlyResultStatus = (status: string | null | undefined): string => {
  const labels: Record<string, string> = {
    SCHEDULED: "已安排发布",
    DRAFT: "草稿已保存",
    COMPLETED: "已完成",
    FAILED: "需要处理",
    CANCELLED: "已取消",
    RUNNING: "执行中",
    QUEUED: "等待执行"
  };
  return labels[String(status || "").toUpperCase()] ?? "状态已更新";
};

const friendlyExecutionFailure = (execution: Execution) => {
  const failedStep = execution.steps?.find(step =>
    step.status === "FAILED" || step.status === "FAILED_RETRYABLE"
  );
  const diagnostic = `${execution.error_code || ""} ${failedStep?.error_code || ""} ${failedStep?.error_message || ""}`.toUpperCase();
  if (/AUTH|TOKEN|PERMISSION|UNAUTHORIZED|FORBIDDEN/.test(diagnostic)) {
    return {
      title: "需要重新授权",
      reason: "当前登录或服务授权已失效。",
      recovery: "重新登录后可以从失败步骤继续。"
    };
  }
  if (/VALIDATION|INVALID|REQUIRED|ARGUMENT/.test(diagnostic)) {
    return {
      title: "需要补充信息",
      reason: "这项操作缺少必要信息，系统没有继续执行。",
      recovery: "补充要求后重新提交即可。"
    };
  }
  if (/TIMEOUT|UNAVAILABLE|CONNECT|CREATOR|JAVA|DEPENDENCY/.test(diagnostic)) {
    return {
      title: "外部服务暂时不可用",
      reason: "任务在调用外部服务时中断，已有内容和发布时间保持不变。",
      recovery: "可以稍后从失败步骤重试。"
    };
  }
  if (/UNKNOWN_SIDE_EFFECT|RECONCILIATION/.test(diagnostic)) {
    return {
      title: "操作状态需要确认",
      reason: "系统暂时无法确认外部操作是否已经生效。",
      recovery: "请先查看任务详情，确认状态后再重试。"
    };
  }
  return {
    title: "任务暂时没有完成",
    reason: "执行过程中遇到了问题，已有结果仍然保留。",
    recovery: "你可以稍后重试，或在任务中心查看详情。"
  };
};

const friendlyClientError = (caught: unknown): string => {
  const message = caught instanceof Error ? caught.message.toUpperCase() : "";
  if (/AUTH|TOKEN|401|403|UNAUTHORIZED|FORBIDDEN/.test(message)) {
    return "登录状态已失效，请重新登录后继续。";
  }
  if (/TIMEOUT|ABORT/.test(message)) return "任务等待超时，可以稍后重试。";
  if (/FETCH|NETWORK|CONNECT|UNAVAILABLE|502|503|504/.test(message)) {
    return "服务暂时无法连接，已有任务状态不会丢失。";
  }
  return "任务暂时无法继续。你可以稍后重试，或前往任务中心查看状态。";
};

const formatResultTime = (runAt: string | null, timezone: string): string | null => {
  if (!runAt) return null;
  const parsed = new Date(runAt);
  if (Number.isNaN(parsed.getTime())) return runAt;
  try {
    return parsed.toLocaleString("zh-CN", {
      timeZone: timezone,
      hour12: false,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    });
  } catch {
    return parsed.toLocaleString("zh-CN", { hour12: false });
  }
};

const ToolPart = ({ part }: { part: AgentToolPart }) => {
  const result = part.result ?? {};
  const draftId = typeof result.draft_id === "string" ? result.draft_id : null;
  const runAt = typeof result.run_at === "string" ? result.run_at : null;
  const results = Array.isArray(result.results) ? result.results as Array<Record<string, unknown>> : [];
  return (
    <div className={styles.toolPart}>
      <span><CheckIcon width={14} height={14} aria-hidden="true" /> {toolLabel(part.tool)}</span>
      <strong>{part.label}</strong>
      {results.length ? (
        <ul>{results.slice(0, 5).map(item => <li key={String(item.id)}>{String(item.title || "未命名帖子")}</li>)}</ul>
      ) : null}
      {draftId ? <small>草稿号 {draftId}</small> : null}
      {runAt ? <small>计划时间 {new Date(runAt).toLocaleString("zh-CN", { hour12: false })}</small> : null}
    </div>
  );
};

export default AgentPanel;
