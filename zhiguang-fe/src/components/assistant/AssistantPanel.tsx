import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAuth } from "@/context/AuthContext";
import AssistantMarkdown from "@/components/content/AssistantMarkdown";
import {
  AssistantIcon,
  CheckIcon,
  ClockIcon,
  CloseIcon,
  SearchIcon,
  SendIcon
} from "@/components/icons/Icon";
import { assistantService, waitForAssistantRun } from "@/services/assistantService";
import { executionService, waitForExecution } from "@/services/executionService";
import type {
  AssistantConversation,
  AssistantEpisode,
  AssistantMemory,
  AssistantMemoryProfile,
  AssistantMessage,
  AssistantRun,
  AssistantToolPart
} from "@/types/assistant";
import type { Execution } from "@/types/execution";
import styles from "./AssistantPanel.module.css";

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

const toolLabel = (tool: string) => {
  if (tool.includes("search")) return "检索社区";
  if (tool.includes("summarize") || tool.includes("get_post")) return "阅读帖子";
  if (tool.includes("creator")) return "调用创作 Agent";
  if (tool.includes("schedule")) return "安排定时发布";
  if (tool.includes("publish")) return "发布帖子";
  return "执行工具";
};

const AssistantPanel = ({ open, onClose, contextPostId, surface = "HOME" }: Props) => {
  const { tokens, isLoading: authLoading } = useAuth();
  const token = tokens?.accessToken;
  const [conversation, setConversation] = useState<AssistantConversation | null>(null);
  const [messages, setMessages] = useState<AssistantMessage[]>([]);
  const [content, setContent] = useState("");
  const [run, setRun] = useState<AssistantRun | null>(null);
  const [execution, setExecution] = useState<Execution | null>(null);
  const [memories, setMemories] = useState<AssistantMemory[]>([]);
  const [episodes, setEpisodes] = useState<AssistantEpisode[]>([]);
  const [memoryProfile, setMemoryProfile] = useState<AssistantMemoryProfile | null>(null);
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
        const existing = await assistantService.listConversations(
          token,
          contextPostId,
          controller.signal
        );
        const current = existing[0] ?? await assistantService.createConversation(token, {
          surface,
          context_post_id: contextPostId
        }, controller.signal);
        if (controller.signal.aborted) return;
        setConversation(current);
        const [nextMessages, nextMemories, nextEpisodes, nextMemoryProfile] = await Promise.all([
          assistantService.listMessages(token, current.conversation_id, controller.signal),
          assistantService.listMemories(token, controller.signal),
          assistantService.listEpisodes(token, controller.signal),
          assistantService.getMemoryProfile(token, controller.signal)
        ]);
        setMessages(nextMessages);
        setMemories(nextMemories);
        setEpisodes(nextEpisodes);
        setMemoryProfile(nextMemoryProfile);
      } catch (caught) {
        if (!controller.signal.aborted) {
          setError(caught instanceof Error ? caught.message : "助手暂时无法连接");
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

  const send = async (suggestion?: string) => {
    const prompt = (suggestion ?? content).trim();
    if (!prompt || !token || !conversation || loading) return;
    const optimistic: AssistantMessage = {
      message_id: crypto.randomUUID(),
      role: "user",
      content: prompt,
      parts: [],
      created_at: new Date().toISOString()
    };
    setMessages(previous => [...previous, optimistic]);
    setContent("");
    setRun(null);
    setExecution(null);
    setError(null);
    setLoading(true);
    runControllerRef.current?.abort();
    const controller = new AbortController();
    runControllerRef.current = controller;
    try {
      const accepted = await assistantService.send(
        token,
        conversation.conversation_id,
        prompt,
        contextPostId
      );
      if (accepted.execution_id) {
        const completed = await waitForExecution(
          token,
          accepted.execution_id,
          setExecution,
          undefined,
          controller.signal
        );
        const failedStep = completed.steps?.find(step =>
          step.status === "FAILED" || step.status === "FAILED_RETRYABLE"
        );
        if (completed.status === "FAILED") {
          throw new Error(failedStep?.error_message || completed.error_message || "Runtime execution failed");
        }
        if (["WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(completed.status)) {
          try {
            setRun(await assistantService.getRun(token, accepted.run_id, controller.signal));
          } catch {
            // Keep the Runtime card when the compatibility projection is unavailable.
          }
          return;
        }
        setMessages(await assistantService.listMessages(token, conversation.conversation_id));
        setRun(null);
        setExecution(null);
        return;
      }
      const completed = await waitForAssistantRun(
        token,
        accepted.run_id,
        setRun,
        controller.signal
      );
      if (completed.status === "FAILED") {
        throw new Error(completed.error || "助手任务执行失败");
      }
      if (["WAITING_APPROVAL", "PAUSED"].includes(completed.status)) {
        setRun(completed);
        return;
      }
      setMessages(await assistantService.listMessages(token, conversation.conversation_id));
      setRun(null);
    } catch (caught) {
      if ((caught as DOMException)?.name !== "AbortError") {
        setError(caught instanceof Error ? caught.message : "助手任务执行失败");
      }
    } finally {
      setLoading(false);
    }
  };

  const decideApproval = async (decision: "APPROVE" | "REJECT") => {
    if (!token || !run?.approval || loading) return;
    setLoading(true);
    setError(null);
    try {
      const updated = await assistantService.decideApproval(
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
          const completed = await waitForAssistantRun(
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
      setMessages(await assistantService.listMessages(token, run.conversation_id));
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
      setRun(await assistantService.cancelRun(token, run.run_id));
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
      setRun(await assistantService.interruptRun(token, run.run_id));
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
          setMessages(await assistantService.listMessages(token, conversation.conversation_id));
        }
        setExecution(null);
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
        ? await assistantService.resumeRun(token, run.run_id)
        : await assistantService.retryRun(token, run.run_id);
      setRun(queued);
      const controller = new AbortController();
      runControllerRef.current = controller;
      const completed = await waitForAssistantRun(token, run.run_id, setRun, controller.signal);
      if (completed.status === "FAILED") throw new Error(completed.error || "任务执行失败");
      if (["WAITING_APPROVAL", "PAUSED"].includes(completed.status)) return;
      setMessages(await assistantService.listMessages(token, run.conversation_id));
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
      const saved = await assistantService.saveMemory(
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
    if (!window.confirm("删除这条助手偏好？删除后无法恢复。")) return;
    try {
      await assistantService.deleteMemory(token, memoryId);
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
      setMemoryProfile(await assistantService.updateMemoryProfile(token, next));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "记忆设置更新失败");
    }
  };

  const deleteEpisode = async (episodeId: string) => {
    if (!token || !window.confirm("删除这条任务记忆？删除后无法恢复。")) return;
    try {
      await assistantService.deleteEpisode(token, episodeId);
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
      await assistantService.clearEpisodes(token);
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
        aria-labelledby="assistant-title"
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
            <span className={styles.mark}><AssistantIcon width={22} height={22} aria-hidden="true" /></span>
            <div>
              <h2 id="assistant-title">GREEN-BOOK 助手</h2>
              <p>能检索、总结、创作，也能替你按时发布</p>
            </div>
          </div>
          <button className={styles.iconButton} type="button" onClick={onClose} aria-label="关闭 GREEN-BOOK 助手">
            <CloseIcon width={20} height={20} />
          </button>
        </header>

        {!token ? (
          <div className={styles.loginState}>
            <AssistantIcon width={34} height={34} aria-hidden="true" />
            <strong>登录后，助手才能代表你执行社区任务</strong>
            <span>它只会在你明确提出时创作或发布内容。</span>
            <Link to="/login">去登录</Link>
          </div>
        ) : (
          <>
            <div className={styles.thread} ref={scrollRef} aria-live="polite">
              <details className={styles.memorySettings}>
                <summary>助手记忆 <small>可查看、关闭和删除</small></summary>
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
                    name="assistant-memory-key"
                    autoComplete="off"
                    placeholder="例如：写作语气…"
                    value={memoryKey}
                    onChange={event => setMemoryKey(event.target.value)}
                  />
                  <input
                    aria-label="偏好内容"
                    name="assistant-memory-value"
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
                  <span className={styles.eyebrow}>社区任务助手</span>
                  <h3>从一句话开始，后面的步骤交给我</h3>
                  <p>我会展示执行进度和真实结果。涉及创作时调用 Creator Agent，帖子数据与发布权限仍由 GREEN-BOOK 负责。</p>
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

              {messages.map(message => (
                <article
                  className={message.role === "user" ? styles.userMessage : styles.assistantMessage}
                  key={message.message_id}
                >
                  {message.role === "assistant" ? (
                    <span className={styles.messageAuthor}><AssistantIcon width={16} height={16} aria-hidden="true" /> GREEN-BOOK 助手</span>
                  ) : null}
                  <div className={styles.messageText}>
                    {message.role === "assistant"
                      ? <AssistantMarkdown content={message.content} />
                      : message.content}
                  </div>
                  {message.parts?.length ? (
                    <details className={styles.executionDetails}>
                      <summary>查看本次执行记录</summary>
                      <div className={styles.toolParts}>
                        {message.parts.map(part => (
                          <ToolPart part={part} key={`${part.tool}-${part.label}`} />
                        ))}
                      </div>
                    </details>
                  ) : null}
                </article>
              ))}

              {execution ? (
                <article className={styles.runCard} data-execution-id={execution.execution_id}>
                  <div className={styles.runHeading}>
                    <span className={execution.status === "CANCELLED" ? styles.stopped : styles.pulse} aria-hidden="true" />
                    <strong>Runtime execution · {execution.status}</strong>
                    {(["PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_APPROVAL"].includes(execution.status)) ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void interruptRun()}>
                        Pause
                      </button>
                    ) : null}
                    {execution.status === "PAUSED" ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void continueRun("resume")}>
                        Resume
                      </button>
                    ) : null}
                    {execution.status === "FAILED" ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void continueRun("retry")}>
                        Retry
                      </button>
                    ) : null}
                    {!["COMPLETED", "FAILED", "CANCELLED"].includes(execution.status) ? (
                      <button className={styles.cancelRun} type="button" onClick={() => void cancelRun()}>
                        Cancel
                      </button>
                    ) : null}
                  </div>
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
                        <span>{step.capability || step.step_id}</span>
                        <small>{step.status}</small>
                      </div>
                    ))}
                  </div>
                  {execution.status === "FAILED" ? (
                    <p className={styles.detailError}>
                      {execution.steps?.find(step => step.error_message)?.error_message || "Runtime execution failed"}
                    </p>
                  ) : null}
                  <details className={styles.runMeta}>
                    <summary>Execution details</summary>
                    <span>execution_id {execution.execution_id}</span>
                    {execution.task_id ? <span>task_id {execution.task_id}</span> : null}
                    {execution.plan_id ? <span>plan_id {execution.plan_id}</span> : null}
                    <span>progress {Math.round(execution.progress * 100)}% ({execution.completed_steps}/{execution.total_steps})</span>
                    <span>events {execution.events?.length ?? 0}</span>
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
                                  ? step.output?.dependency_type === "MODERATION_TASK"
                                    ? "等待审核"
                                    : "等待创作"
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
                          ? <small>将安排 {run.approval.preview.items.length} 篇已审核草稿</small>
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
                name="assistant-message"
                autoComplete="off"
                rows={2}
                disabled={loading}
                aria-label="给 GREEN-BOOK 助手发送消息"
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

const ToolPart = ({ part }: { part: AssistantToolPart }) => {
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

export default AssistantPanel;
