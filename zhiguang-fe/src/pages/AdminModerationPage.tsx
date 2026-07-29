import { useCallback, useEffect, useMemo, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import {
  AlertIcon,
  CheckIcon,
  CloseIcon,
  RefreshIcon,
  ShieldIcon
} from "@/components/icons/Icon";
import { moderationService } from "@/services/moderationService";
import type {
  ModerationAction,
  ModerationCallbackDelivery,
  ModerationStatistics,
  ModerationTask,
  ModerationTaskStatus,
  RiskType
} from "@/types/moderation";
import styles from "./AdminModerationPage.module.css";

const statusLabels: Record<ModerationTaskStatus, string> = {
  PENDING: "等待执行",
  RUNNING: "审核中",
  WAITING_REVIEW: "待人工复审",
  COMPLETED: "已完成",
  FAILED: "执行失败"
};

const riskLabels: Record<RiskType, string> = {
  NORMAL: "正常",
  ADVERTISING: "广告营销",
  ABUSE: "辱骂攻击",
  PRIVACY: "隐私风险"
};

const actionLabels: Record<ModerationAction, string> = {
  PASS: "通过",
  REJECT: "拒绝",
  LIMIT: "限制展示",
  HUMAN_REVIEW: "人工复审"
};

const filters: Array<{ value?: ModerationTaskStatus; label: string }> = [
  { label: "全部任务" },
  { value: "WAITING_REVIEW", label: "待复审" },
  { value: "RUNNING", label: "审核中" },
  { value: "COMPLETED", label: "已完成" },
  { value: "FAILED", label: "失败" }
];

const AdminModerationPage = () => {
  const { user, logout } = useAuth();
  const [filter, setFilter] = useState<ModerationTaskStatus | undefined>("WAITING_REVIEW");
  const [tasks, setTasks] = useState<ModerationTask[]>([]);
  const [selected, setSelected] = useState<ModerationTask | null>(null);
  const [statistics, setStatistics] = useState<ModerationStatistics | null>(null);
  const [callbacks, setCallbacks] = useState<ModerationCallbackDelivery[]>([]);
  const [loading, setLoading] = useState(true);
  const [reviewing, setReviewing] = useState(false);
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (preserveSelection = true) => {
    setLoading(true);
    setError(null);
    try {
      const [nextTasks, nextStatistics, nextCallbacks] = await Promise.all([
        moderationService.tasks(filter),
        moderationService.statistics(),
        moderationService.callbacks()
      ]);
      setTasks(nextTasks);
      setStatistics(nextStatistics);
      setCallbacks(nextCallbacks);
      setSelected(current => {
        if (preserveSelection && current) {
          return nextTasks.find(item => item.id === current.id) ?? nextTasks[0] ?? null;
        }
        return nextTasks[0] ?? null;
      });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "审核数据加载失败");
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    void load(false);
  }, [load]);

  const pendingCount = statistics?.pending_review ?? 0;
  const completedCount = statistics?.by_status?.COMPLETED ?? 0;
  const failedCount = statistics?.by_status?.FAILED ?? 0;
  const callbackAlertCount = callbacks.filter(
    item => item.status === "DEAD" || item.status === "RETRYING"
  ).length;

  const selectedDecision = useMemo(
    () => selected?.agent_decision,
    [selected]
  );

  const submitReview = async (
    action: Exclude<ModerationAction, "HUMAN_REVIEW">
  ) => {
    if (!selected || reviewing) return;
    setReviewing(true);
    setError(null);
    try {
      const result = await moderationService.review(selected.id, {
        action,
        riskType:
          selected.agent_decision?.risk_type ??
          (action === "PASS" ? "NORMAL" : "ABUSE"),
        comment: comment.trim() || undefined,
        expectedVersion: selected.version
      });
      setComment("");
      setSelected(result.task);
      await load(true);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "提交复审失败");
    } finally {
      setReviewing(false);
    }
  };

  return (
    <div className={styles.shell}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}>
          <span className={styles.brandMark}><ShieldIcon /></span>
          <span>
            <strong>GREEN-BOOK 内容安全</strong>
            <small>真实审核工作台</small>
          </span>
        </div>

        <nav className={styles.filters} aria-label="审核任务筛选">
          {filters.map(item => (
            <button
              type="button"
              key={item.label}
              aria-label={item.label}
              className={filter === item.value ? styles.filterActive : styles.filter}
              onClick={() => setFilter(item.value)}
            >
              <span>{item.label}</span>
              {item.value === "WAITING_REVIEW" && pendingCount > 0 ? (
                <strong>{pendingCount}</strong>
              ) : null}
            </button>
          ))}
        </nav>

        <div className={styles.account}>
          <span>{user?.nickname?.slice(0, 1) || "管"}</span>
          <div>
            <strong>{user?.nickname || "管理员"}</strong>
            <small>管理员</small>
          </div>
          <button type="button" onClick={() => void logout()}>退出</button>
        </div>
      </aside>

      <main className={styles.workspace}>
        <header className={styles.header}>
          <div>
            <span className={styles.eyebrow}>MODERATION AGENT</span>
            <h1>内容审核</h1>
            <p>AI 先完成风险判断，只有需要人工确认的内容才会进入这里。</p>
          </div>
          <button
            type="button"
            className={styles.refresh}
            onClick={() => void load(true)}
            disabled={loading}
          >
            <RefreshIcon aria-hidden="true" />
            {loading ? "刷新中" : "刷新"}
          </button>
        </header>

        <section className={styles.stats} aria-label="审核统计">
          <article>
            <span>待人工复审</span>
            <strong>{pendingCount}</strong>
          </article>
          <article>
            <span>累计任务</span>
            <strong>{statistics?.total_tasks ?? 0}</strong>
          </article>
          <article>
            <span>已完成</span>
            <strong>{completedCount}</strong>
          </article>
          <article>
            <span>失败</span>
            <strong>{failedCount}</strong>
          </article>
          <article>
            <span>回调待处理</span>
            <strong>{callbackAlertCount}</strong>
          </article>
        </section>

        {error ? (
          <div className={styles.error} role="alert">
            <AlertIcon aria-hidden="true" />
            <span>{error}</span>
            <button type="button" onClick={() => void load(true)}>重试</button>
          </div>
        ) : null}

        <div className={styles.reviewGrid}>
          <section className={styles.taskList} aria-label="审核任务列表">
            <div className={styles.listHeader}>
              <strong>{filters.find(item => item.value === filter)?.label}</strong>
              <span>{tasks.length} 条</span>
            </div>
            {loading && tasks.length === 0 ? (
              <div className={styles.empty}>正在读取审核任务…</div>
            ) : tasks.length === 0 ? (
              <div className={styles.empty}>当前没有需要处理的内容</div>
            ) : (
              tasks.map(task => (
                <button
                  type="button"
                  key={task.id}
                  className={selected?.id === task.id ? styles.taskActive : styles.task}
                  onClick={() => setSelected(task)}
                >
                  <span className={`${styles.statusDot} ${styles[`status${task.status}`]}`} />
                  <span className={styles.taskCopy}>
                    <strong>{task.content.split("\n")[0].replace(/^标题：/, "") || "未命名内容"}</strong>
                    <small>{task.content.slice(0, 88)}</small>
                    <span>
                      {statusLabels[task.status]} · {new Date(task.updated_at).toLocaleString("zh-CN")}
                    </span>
                  </span>
                </button>
              ))
            )}
          </section>

          <section className={styles.detail} aria-live="polite">
            {!selected ? (
              <div className={styles.empty}>从左侧选择一条内容查看详情</div>
            ) : (
              <>
                <div className={styles.detailHeader}>
                  <div>
                    <span className={styles.statusPill}>{statusLabels[selected.status]}</span>
                    <h2>{selected.content.split("\n")[0].replace(/^标题：/, "") || "内容详情"}</h2>
                    <p>
                      内容 ID {selected.content_id || "—"} · 作者 {selected.creator_id || "—"}
                    </p>
                  </div>
                  {selectedDecision ? (
                    <div className={styles.score}>
                      <span>风险分</span>
                      <strong>{Math.round(selectedDecision.risk_score * 100)}</strong>
                    </div>
                  ) : null}
                </div>

                {selectedDecision ? (
                  <div className={styles.decision}>
                    <div>
                      <span>Agent 建议</span>
                      <strong>{actionLabels[selectedDecision.recommended_action]}</strong>
                    </div>
                    <div>
                      <span>风险类型</span>
                      <strong>{riskLabels[selectedDecision.risk_type]}</strong>
                    </div>
                    <div>
                      <span>置信度</span>
                      <strong>{Math.round(selectedDecision.confidence * 100)}%</strong>
                    </div>
                    <p>{selectedDecision.reason}</p>
                  </div>
                ) : null}

                <article className={styles.content}>
                  <h3>送审原文</h3>
                  <pre>{selected.content}</pre>
                </article>

                {selected.status === "WAITING_REVIEW" ? (
                  <div className={styles.reviewPanel}>
                    <label htmlFor="review-comment">复审说明（可选）</label>
                    <textarea
                      id="review-comment"
                      value={comment}
                      onChange={event => setComment(event.target.value)}
                      placeholder="记录判断依据，便于后续追溯"
                      maxLength={2000}
                    />
                    <div className={styles.reviewActions}>
                      <button
                        type="button"
                        className={styles.pass}
                        disabled={reviewing}
                        onClick={() => void submitReview("PASS")}
                      >
                        <CheckIcon aria-hidden="true" />通过
                      </button>
                      <button
                        type="button"
                        className={styles.limit}
                        disabled={reviewing}
                        onClick={() => void submitReview("LIMIT")}
                      >
                        <AlertIcon aria-hidden="true" />限制展示
                      </button>
                      <button
                        type="button"
                        className={styles.reject}
                        disabled={reviewing}
                        onClick={() => void submitReview("REJECT")}
                      >
                        <CloseIcon aria-hidden="true" />拒绝
                      </button>
                    </div>
                  </div>
                ) : null}

                <details className={styles.technical}>
                  <summary>技术详情</summary>
                  <dl>
                    <div><dt>任务 ID</dt><dd>{selected.id}</dd></div>
                    <div><dt>线程 ID</dt><dd>{selected.thread_id}</dd></div>
                    <div><dt>追踪 ID</dt><dd>{selected.trace_id || "—"}</dd></div>
                    <div><dt>版本</dt><dd>{selected.version}</dd></div>
                    <div>
                      <dt>平台</dt>
                      <dd>
                        {selected.platform?.toLowerCase() === "zhiguang"
                          ? "GREEN-BOOK"
                          : selected.platform || "GREEN-BOOK"}
                      </dd>
                    </div>
                  </dl>
                </details>
              </>
            )}
          </section>
        </div>

        <details className={styles.callbackOps}>
          <summary>
            结果投递运维
            <span>{callbackAlertCount > 0 ? `${callbackAlertCount} 条需关注` : "运行正常"}</span>
          </summary>
          <div>
            {callbacks.length === 0 ? (
              <p>还没有审核结果投递记录。</p>
            ) : callbacks.map(item => (
              <article key={item.id}>
                <strong>{item.status}</strong>
                <span>任务 {item.task_id.slice(0, 8)}</span>
                <span>尝试 {item.attempts}/{item.max_attempts}</span>
                <small>{item.last_error || new Date(item.updated_at).toLocaleString("zh-CN")}</small>
              </article>
            ))}
          </div>
        </details>
      </main>
    </div>
  );
};

export default AdminModerationPage;
