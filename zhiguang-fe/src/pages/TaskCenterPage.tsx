import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, Navigate, useLocation, useSearchParams } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import AuthStatus from "@/features/auth/AuthStatus";
import {
  AlertIcon,
  AssistantIcon,
  CheckIcon,
  ClockIcon,
  CreateIcon,
  RefreshIcon,
  ShieldIcon,
  SparkIcon
} from "@/components/icons/Icon";
import { useAuth } from "@/context/AuthContext";
import { assistantService } from "@/services/assistantService";
import { executionService } from "@/services/executionService";
import { creatorTaskService } from "@/services/creatorTaskService";
import { knowpostService } from "@/services/knowpostService";
import type { AssistantRun, AssistantRunListItem, AssistantScheduledAction } from "@/types/assistant";
import type { Execution } from "@/types/execution";
import type { CreatorTaskListItem, PostTaskItem } from "@/types/task";
import styles from "./TaskCenterPage.module.css";

type TaskView = "all" | "active" | "attention" | "completed";
type TaskGroup = Exclude<TaskView, "all">;
type TaskSource = "assistant" | "creator" | "schedule" | "publication";

type UnifiedTask = {
  key: string;
  source: TaskSource;
  sourceLabel: string;
  title: string;
  description: string;
  status: string;
  statusLabel: string;
  group: TaskGroup;
  updatedAt: string;
  createdAt: string;
  progress?: number;
  raw: Execution | CreatorTaskListItem | AssistantScheduledAction | PostTaskItem;
};

const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit"
});

const formatDate = (value: string) => {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "时间未知" : dateFormatter.format(parsed);
};

const assistantStatus = (status: AssistantRun["status"]): [string, TaskGroup] => {
  const statuses: Record<AssistantRun["status"], [string, TaskGroup]> = {
    QUEUED: ["排队中", "active"],
    RUNNING: ["执行中", "active"],
    RETRYING: ["重试中", "active"],
    WAITING_DEPENDENCY: ["等待依赖", "active"],
    WAITING_LANE: ["等待执行通道", "active"],
    WAITING_APPROVAL: ["等待确认", "attention"],
    PAUSED: ["已暂停", "attention"],
    COMPLETED: ["已完成", "completed"],
    FAILED: ["执行失败", "attention"],
    CANCELLED: ["已终止", "completed"]
  };
  return statuses[status];
};

const executionStatus = (status: Execution["status"]): [string, TaskGroup] => {
  switch (status) {
    case "PENDING":
    case "RUNNING":
      return ["Runtime running", "active"];
    case "WAITING_APPROVAL":
    case "WAITING_HUMAN":
      return ["Waiting for approval", "attention"];
    case "PAUSED":
      return ["Paused", "attention"];
    case "FAILED":
      return ["Execution failed", "attention"];
    case "CANCELLED":
      return ["Cancelled", "completed"];
    case "COMPLETED":
      return ["Completed", "completed"];
    default:
      return [status, "active"];
  }
};

const creatorStatus = (status: CreatorTaskListItem["status"]): [string, TaskGroup] => {
  const statuses: Record<CreatorTaskListItem["status"], [string, TaskGroup]> = {
    CREATED: ["准备中", "active"],
    QUEUED: ["排队中", "active"],
    RUNNING: ["创作中", "active"],
    WAITING_HUMAN: ["等待修改", "attention"],
    RETRYING: ["重试中", "active"],
    COMPLETED: ["已完成", "completed"],
    FAILED: ["创作失败", "attention"],
    CANCELLED: ["已终止", "completed"]
  };
  return statuses[status];
};

const scheduleStatus = (status: string): [string, TaskGroup] => {
  switch (status.toUpperCase()) {
    case "PENDING":
    case "SCHEDULED":
      return ["等待发布时间", "active"];
    case "PROCESSING":
    case "RUNNING":
      return ["正在发布", "active"];
    case "RETRYING":
      return ["发布重试中", "active"];
    case "FAILED":
      return ["发布失败", "attention"];
    case "CANCELLED":
      return ["已取消", "completed"];
    case "COMPLETED":
      return ["已发布", "completed"];
    default:
      return ["状态待同步", "attention"];
  }
};

const publicationStatus = (status: PostTaskItem["status"]): [string, TaskGroup] => {
  const statuses: Record<PostTaskItem["status"], [string, TaskGroup]> = {
    draft: ["草稿", "attention"],
    reviewing: ["审核中", "active"],
    published: ["已发布", "completed"],
    rejected: ["需修改", "attention"]
  };
  return statuses[status];
};

const sourceIcon = (source: TaskSource) => {
  switch (source) {
    case "assistant":
      return <AssistantIcon aria-hidden="true" />;
    case "creator":
      return <SparkIcon aria-hidden="true" />;
    case "schedule":
      return <ClockIcon aria-hidden="true" />;
    case "publication":
      return <CreateIcon aria-hidden="true" />;
  }
};

const toUnifiedTasks = (
  executions: Execution[],
  creatorTasks: CreatorTaskListItem[],
  schedules: AssistantScheduledAction[],
  posts: PostTaskItem[]
): UnifiedTask[] => {
  const assistantTasks = executions.map<UnifiedTask>(execution => {
    const [statusLabel, group] = executionStatus(execution.status);
    return {
      key: `execution:${execution.execution_id}`,
      source: "assistant",
      sourceLabel: "助手任务",
      title: execution.current_step || execution.task_id || "Assistant execution",
      description: `execution_id ${execution.execution_id}`,
      status: execution.status,
      statusLabel,
      group,
      updatedAt: execution.updated_at,
      createdAt: execution.created_at,
      progress: Math.round(execution.progress * 100),
      raw: execution
    };
  });

  const independentCreatorTasks = creatorTasks.map<UnifiedTask>(task => {
      const [statusLabel, group] = creatorStatus(task.status);
      return {
        key: `creator:${task.task_id}`,
        source: "creator",
        sourceLabel: "AI 创作",
        title: task.goal || "AI 创作任务",
        description: task.error_code ? `创作未完成：${task.error_code}` : "由创作 Agent 研究、组织并生成内容。",
        status: task.status,
        statusLabel,
        group,
        updatedAt: task.updated_at,
        createdAt: task.created_at,
        raw: task
      };
    });

  const scheduledTasks = schedules.map<UnifiedTask>(action => {
    const [statusLabel, group] = scheduleStatus(action.status);
    return {
      key: `schedule:${action.action_id}`,
      source: "schedule",
      sourceLabel: "定时发布",
      title: action.instruction || "定时发布帖子",
      description: `计划于 ${formatDate(action.run_at)} 执行`,
      status: action.status,
      statusLabel,
      group,
      updatedAt: action.run_at,
      createdAt: action.run_at,
      raw: action
    };
  });

  const publicationTasks = posts.map<UnifiedTask>(post => {
    const [statusLabel, group] = publicationStatus(post.status);
    const origin = post.contentOrigin === "AI_ASSISTED" ? "AI 协作内容" : "自主创作内容";
    return {
      key: `publication:${post.id}`,
      source: "publication",
      sourceLabel: "帖子发布",
      title: post.title?.trim() || "未命名帖子",
      description: post.reason || `${origin}的发布流程`,
      status: post.status,
      statusLabel,
      group,
      updatedAt: post.updatedAt,
      createdAt: post.createdAt,
      raw: post
    };
  });

  return [...assistantTasks, ...independentCreatorTasks, ...scheduledTasks, ...publicationTasks]
    .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
};

const TaskCenterPage = () => {
  const { tokens, isLoading: authLoading } = useAuth();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedView = searchParams.get("view");
  const view: TaskView = ["active", "attention", "completed"].includes(requestedView ?? "")
    ? requestedView as TaskView
    : "all";
  const requestedPage = Number(searchParams.get("page") ?? "1");

  const [executions, setExecutions] = useState<Execution[]>([]);
  const [creatorTasks, setCreatorTasks] = useState<CreatorTaskListItem[]>([]);
  const [schedules, setSchedules] = useState<AssistantScheduledAction[]>([]);
  const [posts, setPosts] = useState<PostTaskItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [serviceErrors, setServiceErrors] = useState<string[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);

  const loadTasks = useCallback(async (silent = false) => {
    if (!tokens?.accessToken) return;
    if (silent) setRefreshing(true);
    else setLoading(true);

    const results = await Promise.allSettled([
      executionService.list(tokens.accessToken),
      creatorTaskService.list(tokens.accessToken),
      assistantService.scheduledActions(tokens.accessToken),
      knowpostService.taskItems(tokens.accessToken)
    ]);
    const errors: string[] = [];

    if (results[0].status === "fulfilled") setExecutions(results[0].value.items ?? []);
    else errors.push("助手 Agent");

    if (results[1].status === "fulfilled") setCreatorTasks(results[1].value.items ?? []);
    else errors.push("创作 Agent");

    if (results[2].status === "fulfilled") setSchedules(results[2].value);
    else if (!errors.includes("助手 Agent")) errors.push("定时任务");

    if (results[3].status === "fulfilled") setPosts(results[3].value);
    else errors.push("Java 发布服务");

    setServiceErrors(errors);
    setLoading(false);
    setRefreshing(false);
  }, [tokens?.accessToken]);

  useEffect(() => {
    void loadTasks();
  }, [loadTasks]);

  const tasks = useMemo(
    () => toUnifiedTasks(executions, creatorTasks, schedules, posts),
    [executions, creatorTasks, schedules, posts]
  );
  const hasRunningTasks = tasks.some(task => task.group === "active");

  useEffect(() => {
    if (!hasRunningTasks) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void loadTasks(true);
    }, 8_000);
    return () => window.clearInterval(timer);
  }, [hasRunningTasks, loadTasks]);

  const counts = useMemo(() => ({
    all: tasks.length,
    active: tasks.filter(task => task.group === "active").length,
    attention: tasks.filter(task => task.group === "attention").length,
    completed: tasks.filter(task => task.group === "completed").length,
    scheduled: schedules.filter(action =>
      ["PENDING", "SCHEDULED", "PROCESSING", "RUNNING", "RETRYING"].includes(action.status.toUpperCase())
    ).length
  }), [schedules, tasks]);

  const filteredTasks = view === "all" ? tasks : tasks.filter(task => task.group === view);
  const pageSize = 20;
  const pageCount = Math.max(1, Math.ceil(filteredTasks.length / pageSize));
  const page = Math.min(
    Number.isInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1,
    pageCount
  );
  const visibleTasks = filteredTasks.slice((page - 1) * pageSize, page * pageSize);

  useEffect(() => {
    if (loading || (Number.isInteger(requestedPage) && requestedPage === page)) return;
    const next = new URLSearchParams(searchParams);
    if (page <= 1) next.delete("page");
    else next.set("page", String(page));
    setSearchParams(next, { replace: true });
  }, [loading, page, requestedPage, searchParams, setSearchParams]);

  const selectView = (nextView: string) => {
    const next = new URLSearchParams(searchParams);
    next.delete("page");
    if (nextView === "all") next.delete("view");
    else next.set("view", nextView);
    setSearchParams(next, { replace: true });
  };

  const selectPage = (nextPage: number) => {
    const next = new URLSearchParams(searchParams);
    if (nextPage <= 1) next.delete("page");
    else next.set("page", String(nextPage));
    setSearchParams(next);
  };

  const execute = async (key: string, successMessage: string, operation: () => Promise<unknown>) => {
    setBusyKey(key);
    setNotice(null);
    try {
      await operation();
      setNotice(successMessage);
      await loadTasks(true);
    } catch (error) {
      setNotice(error instanceof Error ? `${error.message}，请稍后重试。` : "操作失败，请稍后重试。");
    } finally {
      setBusyKey(null);
    }
  };

  if (authLoading) {
    return <div className={styles.routeLoading} role="status">正在加载任务…</div>;
  }
  if (!tokens?.accessToken) {
    return <Navigate to="/login" replace state={{ from: `/tasks${location.search}` }} />;
  }

  const renderActions = (task: UnifiedTask) => {
    const isBusy = busyKey === task.key;
    if (task.source === "assistant") {
      const execution = task.raw as Execution;
      if (execution.status === "FAILED") {
        return (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={isBusy || !execution.current_step}
            onClick={() => execute(task.key, "Runtime execution queued for retry", () =>
              executionService.retryStep(tokens.accessToken, execution.execution_id, execution.current_step)
            )}
          >
            {isBusy ? "Retrying" : "Retry step"}
          </button>
        );
      }
      if (execution.status === "PAUSED") {
        return (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={isBusy}
            onClick={() => execute(task.key, "Runtime execution resumed", () =>
              executionService.resume(tokens.accessToken, execution.execution_id)
            )}
          >
            {isBusy ? "Resuming" : "Resume"}
          </button>
        );
      }
      if (["PENDING", "RUNNING", "WAITING_HUMAN", "WAITING_APPROVAL"].includes(execution.status)) {
        return (
          <>
            <button
              type="button"
              className={styles.secondaryAction}
              disabled={isBusy}
              onClick={() => execute(task.key, "Runtime execution paused", () =>
                executionService.pause(tokens.accessToken, execution.execution_id)
              )}
            >
              {isBusy ? "Pausing" : "Pause"}
            </button>
            <button
              type="button"
              className={styles.dangerAction}
              disabled={isBusy}
              onClick={() => {
                if (window.confirm("Cancel this Runtime execution?")) {
                  void execute(task.key, "Runtime execution cancelled", () =>
                    executionService.cancel(tokens.accessToken, execution.execution_id)
                  );
                }
              }}
            >
              Cancel
            </button>
          </>
        );
      }
      return null;
    }

    if ((task.source as string) === "assistant") {
      const run = task.raw as unknown as AssistantRunListItem;
      if (run.status === "WAITING_APPROVAL" && run.approval) {
        return (
          <>
            <button
              type="button"
              className={styles.primaryAction}
              disabled={isBusy}
              onClick={() => execute(task.key, "已确认，助手将继续执行。", () =>
                assistantService.decideApproval(
                  tokens.accessToken,
                  run.run_id,
                  run.approval!.approval_id,
                  "APPROVE",
                  run.approval!.expected_run_version
                )
              )}
            >
              {isBusy ? "处理中…" : "确认执行"}
            </button>
            <button
              type="button"
              className={styles.secondaryAction}
              disabled={isBusy}
              onClick={() => execute(task.key, "已拒绝本次操作。", () =>
                assistantService.decideApproval(
                  tokens.accessToken,
                  run.run_id,
                  run.approval!.approval_id,
                  "REJECT",
                  run.approval!.expected_run_version
                )
              )}
            >
              拒绝
            </button>
          </>
        );
      }
      if (run.status === "FAILED") {
        return (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={isBusy}
            onClick={() => execute(task.key, "任务已重新排队。", () =>
              assistantService.retryRun(tokens.accessToken, run.run_id)
            )}
          >
            {isBusy ? "重试中…" : "重试任务"}
          </button>
        );
      }
      if (run.status === "PAUSED") {
        return (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={isBusy}
            onClick={() => execute(task.key, "任务已继续执行。", () =>
              assistantService.resumeRun(tokens.accessToken, run.run_id)
            )}
          >
            {isBusy ? "恢复中…" : "继续执行"}
          </button>
        );
      }
      if (["QUEUED", "RUNNING", "RETRYING", "WAITING_DEPENDENCY", "WAITING_LANE"].includes(run.status)) {
        return (
          <>
            <button
              type="button"
              className={styles.secondaryAction}
              disabled={isBusy}
              onClick={() => execute(task.key, "任务已暂停。", () =>
                assistantService.interruptRun(tokens.accessToken, run.run_id)
              )}
            >
              {isBusy ? "处理中…" : "暂停"}
            </button>
            <button
              type="button"
              className={styles.dangerAction}
              disabled={isBusy}
              onClick={() => {
                if (window.confirm("确定终止这个任务吗？已完成的步骤会保留。")) {
                  void execute(task.key, "任务已终止。", () =>
                    assistantService.cancelRun(tokens.accessToken, run.run_id)
                  );
                }
              }}
            >
              终止
            </button>
          </>
        );
      }
    }

    if (task.source === "creator") {
      const creator = task.raw as CreatorTaskListItem;
      if (creator.status === "WAITING_HUMAN") {
        return <Link className={styles.primaryAction} to="/create/ai">继续创作</Link>;
      }
      if (creator.status === "FAILED") {
        return (
          <button
            type="button"
            className={styles.primaryAction}
            disabled={isBusy}
            onClick={() => execute(task.key, "创作任务已重新排队。", () =>
              creatorTaskService.retry(tokens.accessToken, creator.task_id, creator.version)
            )}
          >
            {isBusy ? "重试中…" : "重试创作"}
          </button>
        );
      }
      if (["CREATED", "QUEUED", "RUNNING", "RETRYING"].includes(creator.status)) {
        return (
          <button
            type="button"
            className={styles.dangerAction}
            disabled={isBusy}
            onClick={() => {
              if (window.confirm("确定取消这个创作任务吗？")) {
                void execute(task.key, "创作任务已取消。", () =>
                  creatorTaskService.cancel(tokens.accessToken, creator.task_id, creator.version)
                );
              }
            }}
          >
            {isBusy ? "取消中…" : "取消任务"}
          </button>
        );
      }
    }

    if (task.source === "schedule") {
      const schedule = task.raw as AssistantScheduledAction;
      if (["PENDING", "SCHEDULED", "RETRYING"].includes(schedule.status.toUpperCase())) {
        return (
          <button
            type="button"
            className={styles.dangerAction}
            disabled={isBusy}
            onClick={() => {
              if (window.confirm("确定取消这次定时发布吗？")) {
                void execute(task.key, "定时发布已取消。", () =>
                  assistantService.cancelScheduledAction(tokens.accessToken, schedule.action_id)
                );
              }
            }}
          >
            {isBusy ? "取消中…" : "取消发布"}
          </button>
        );
      }
    }

    if (task.source === "publication") {
      const post = task.raw as PostTaskItem;
      if (post.status === "draft" || post.status === "rejected") {
        return (
          <Link className={styles.primaryAction} to={`/create/manual?draftId=${encodeURIComponent(post.id)}`}>
            {post.status === "rejected" ? "修改后重发" : "继续编辑"}
          </Link>
        );
      }
      if (post.status === "published") {
        return <Link className={styles.secondaryAction} to={`/post/${encodeURIComponent(post.id)}`}>查看帖子</Link>;
      }
    }
    return null;
  };

  const renderDetails = (task: UnifiedTask) => {
    if (task.source === "assistant") {
      const execution = task.raw as Execution;
      return (
        <details className={styles.details}>
          <summary>Runtime execution details</summary>
          <div className={styles.detailBody}>
            <p className={styles.trace}>execution_id {execution.execution_id}</p>
            {execution.task_id ? <p>task_id {execution.task_id}</p> : null}
            {execution.plan_id ? <p>plan_id {execution.plan_id}</p> : null}
            <p>current step {execution.current_step || "-"}</p>
            <p>progress {Math.round(execution.progress * 100)}% ({execution.completed_steps}/{execution.total_steps})</p>
          </div>
        </details>
      );
    }
    if ((task.source as string) === "assistant") {
      const run = task.raw as unknown as AssistantRunListItem;
      return (
        <details className={styles.details}>
          <summary>运行详情</summary>
          <div className={styles.detailBody}>
            {run.steps.length ? (
              <ol className={styles.stepList}>
                {run.steps.map(step => (
                  <li key={step.step_id}>
                    <span>{step.label}</span>
                    <small>{assistantStatus(
                      step.status === "PENDING" ? "QUEUED"
                        : step.status === "WAITING_APPROVAL" ? "WAITING_APPROVAL"
                          : step.status === "WAITING_DEPENDENCY" ? "WAITING_DEPENDENCY"
                            : step.status === "FAILED" ? "FAILED"
                              : step.status === "CANCELLED" ? "CANCELLED"
                                : step.status === "COMPLETED" ? "COMPLETED"
                                  : "RUNNING"
                    )[0]}</small>
                  </li>
                ))}
              </ol>
            ) : <p>任务尚未生成执行步骤。</p>}
            {run.error ? <p className={styles.detailError}>{run.error}</p> : null}
            <p className={styles.trace}>追踪号 {run.trace_id}</p>
          </div>
        </details>
      );
    }
    if (task.source === "creator") {
      const creator = task.raw as CreatorTaskListItem;
      return (
        <details className={styles.details}>
          <summary>运行详情</summary>
          <div className={styles.detailBody}>
            <p>任务类型：{creator.kind}</p>
            <p>当前版本：{creator.version}</p>
            <p className={styles.trace}>任务号 {creator.task_id}</p>
          </div>
        </details>
      );
    }
    if (task.source === "schedule") {
      const schedule = task.raw as AssistantScheduledAction;
      return (
        <details className={styles.details}>
          <summary>发布详情</summary>
          <div className={styles.detailBody}>
            <p>已尝试 {schedule.attempts ?? 0} 次</p>
            {schedule.error ? <p className={styles.detailError}>{schedule.error}</p> : null}
            <p className={styles.trace}>任务号 {schedule.action_id}</p>
          </div>
        </details>
      );
    }
    const post = task.raw as PostTaskItem;
    if (!post.moderationTaskId && !post.reason) return null;
    return (
      <details className={styles.details}>
        <summary>审核详情</summary>
        <div className={styles.detailBody}>
          {post.reason ? <p>{post.reason}</p> : <p>审核 Agent 正在检查内容。</p>}
          {post.moderationTaskId ? <p className={styles.trace}>审核任务 {post.moderationTaskId}</p> : null}
        </div>
      </details>
    );
  };

  return (
    <AppLayout
      variant="cardless"
      header={
        <MainHeader
          headline="任务"
          subtitle="创作、审核与定时发布都在这里持续推进，你只需要处理真正需要确认的节点。"
          rightSlot={<AuthStatus />}
          filters={[
            { id: "all", label: "全部", badge: counts.all, active: view === "all", onSelect: selectView },
            { id: "active", label: "进行中", badge: counts.active, active: view === "active", onSelect: selectView },
            { id: "attention", label: "待处理", badge: counts.attention, active: view === "attention", onSelect: selectView },
            { id: "completed", label: "已完成", badge: counts.completed, active: view === "completed", onSelect: selectView }
          ]}
        />
      }
    >
      <section className={styles.overview} aria-label="任务概览">
        <button type="button" className={styles.metric} onClick={() => selectView("active")}>
          <span className={styles.metricIcon}><ClockIcon aria-hidden="true" /></span>
          <span><strong>{counts.active}</strong><small>正在推进</small></span>
        </button>
        <button type="button" className={styles.metric} onClick={() => selectView("attention")}>
          <span className={`${styles.metricIcon} ${styles.attentionIcon}`}><AlertIcon aria-hidden="true" /></span>
          <span><strong>{counts.attention}</strong><small>需要你处理</small></span>
        </button>
        <button type="button" className={styles.metric} onClick={() => selectView("all")}>
          <span className={styles.metricIcon}><SparkIcon aria-hidden="true" /></span>
          <span><strong>{counts.scheduled}</strong><small>等待定时发布</small></span>
        </button>
        <button type="button" className={styles.metric} onClick={() => selectView("completed")}>
          <span className={styles.metricIcon}><CheckIcon aria-hidden="true" /></span>
          <span><strong>{counts.completed}</strong><small>已经完成</small></span>
        </button>
      </section>

      <div className={styles.toolbar}>
        <div>
          <h2>{view === "all" ? "最近任务" : view === "active" ? "进行中的任务" : view === "attention" ? "等待你处理" : "已完成的任务"}</h2>
          <p>任务变化时会自动刷新。</p>
        </div>
        <button
          type="button"
          className={styles.refreshButton}
          disabled={refreshing}
          onClick={() => void loadTasks(true)}
        >
          <RefreshIcon aria-hidden="true" />
          {refreshing ? "刷新中…" : "刷新"}
        </button>
      </div>

      {notice ? <div className={styles.notice} role="status" aria-live="polite">{notice}</div> : null}
      {serviceErrors.length ? (
        <div className={styles.partialError} role="status">
          <AlertIcon aria-hidden="true" />
          <span>
            {serviceErrors.join("、")}暂未连接，已展示其余真实任务。请确认对应服务已启动后刷新。
          </span>
        </div>
      ) : null}

      {loading ? (
        <div className={styles.skeletonList} aria-label="正在加载任务" role="status">
          {[0, 1, 2].map(item => <span key={item} />)}
        </div>
      ) : (
        <section className={styles.taskList}>
          {visibleTasks.map(task => (
            <article key={task.key} className={styles.taskCard}>
              <div className={`${styles.sourceIcon} ${styles[`source_${task.source}`]}`}>
                {sourceIcon(task.source)}
              </div>
              <div className={styles.taskContent}>
                <div className={styles.taskMeta}>
                  <span>{task.sourceLabel}</span>
                  <time dateTime={task.updatedAt}>{formatDate(task.updatedAt)}</time>
                </div>
                <div className={styles.taskHeading}>
                  <h3>{task.title}</h3>
                  <span className={`${styles.status} ${styles[`status_${task.group}`]}`}>
                    {task.statusLabel}
                  </span>
                </div>
                <p className={styles.description}>{task.description}</p>
                {task.progress !== undefined && task.group === "active" ? (
                  <div
                    className={styles.progress}
                    role="progressbar"
                    aria-label="任务进度"
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={task.progress}
                  >
                    <span style={{ transform: `scaleX(${task.progress / 100})` }} />
                  </div>
                ) : null}
                <div className={styles.cardFooter}>
                  <div className={styles.actions}>{renderActions(task)}</div>
                  {renderDetails(task)}
                </div>
              </div>
            </article>
          ))}

          {!filteredTasks.length ? (
            <div className={styles.empty}>
              <span className={styles.emptyIcon}><ShieldIcon aria-hidden="true" /></span>
              <h2>{tasks.length ? "这个分类暂时没有任务" : "还没有任务"}</h2>
              <p>{tasks.length ? "切换到“全部”查看其他进度。" : "让助手执行一个目标，或开始一次 AI 创作，任务会自动出现在这里。"}</p>
              <div className={styles.emptyActions}>
                {tasks.length ? (
                  <button type="button" className={styles.primaryAction} onClick={() => selectView("all")}>查看全部任务</button>
                ) : (
                  <>
                    <Link className={styles.primaryAction} to="/">打开社区助手</Link>
                    <Link className={styles.secondaryAction} to="/create/ai">开始 AI 创作</Link>
                  </>
                )}
              </div>
            </div>
          ) : null}

          {filteredTasks.length > pageSize ? (
            <nav className={styles.pagination} aria-label="任务分页">
              <button type="button" disabled={page <= 1} onClick={() => selectPage(page - 1)}>
                上一页
              </button>
              <span aria-live="polite">第 {page} / {pageCount} 页</span>
              <button type="button" disabled={page >= pageCount} onClick={() => selectPage(page + 1)}>
                下一页
              </button>
            </nav>
          ) : null}
        </section>
      )}
    </AppLayout>
  );
};

export default TaskCenterPage;
