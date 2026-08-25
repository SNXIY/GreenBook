import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  CheckIcon,
  ClockIcon,
  RefreshIcon,
  TaskIcon
} from "@/components/icons/Icon";
import type { AgentRun } from "@/types/agent";
import type { Execution } from "@/types/execution";
import {
  projectExecutionActivity,
  projectRunActivity,
  userFacingStepLabel,
  userFacingStatusLabel,
  type AgentActivityItem,
  type UserFacingApprovalRequest
} from "./userFacingResult";
import { runtimeStepStatusLabel } from "@/services/runtimeExecutionLabels";
import styles from "./AgentActivityCards.module.css";

type ExecutionHandlers = {
  onPause: () => void;
  onResume: () => void;
  onRetry: () => void;
  onCancel: () => void;
};

type ActivityListProps = {
  items: AgentActivityItem[];
};

const ActivityList = ({ items }: ActivityListProps) => (
  <ul className={styles.activityList} aria-live="polite">
    {items.slice(0, 4).map(item => (
      <li key={item.id} className={styles[`activity${item.status[0].toUpperCase()}${item.status.slice(1)}`]}>
        <span className={styles.activityMarker} aria-hidden="true">
          {item.status === "complete" ? <CheckIcon width={14} height={14} /> : null}
        </span>
        <span>{item.label}</span>
      </li>
    ))}
  </ul>
);

const ActivityControls = ({
  execution,
  run,
  handlers,
  disabled
}: {
  execution?: Execution;
  run?: AgentRun;
  handlers: ExecutionHandlers;
  disabled: boolean;
}) => {
  const status = execution?.status || run?.status || "";
  const controlState = execution?.control_state || "";
  const verifyingResult = String(execution?.error_code || "").toUpperCase() === "RESULT_UNKNOWN";
  const canPause = execution
    ? controlState === "RUNNING" && ["PENDING", "RUNNING"].includes(status)
    : ["QUEUED", "RUNNING", "RETRYING", "WAITING_DEPENDENCY", "WAITING_LANE"].includes(status);
  const canResume = execution
    ? controlState === "PAUSED" || status === "PAUSED"
    : status === "PAUSED";
  const canRetry = status === "FAILED" && !verifyingResult;
  const canCancel = !["COMPLETED", "FAILED", "CANCELLED"].includes(status) && !canResume;

  return (
    <div className={styles.controls}>
      {canPause ? (
        <button type="button" onClick={handlers.onPause} disabled={disabled}>暂停</button>
      ) : null}
      {canResume ? (
        <button type="button" onClick={handlers.onResume} disabled={disabled}>继续</button>
      ) : null}
      {canRetry ? (
        <button type="button" onClick={handlers.onRetry} disabled={disabled}>
          <RefreshIcon width={14} height={14} aria-hidden="true" />重试
        </button>
      ) : null}
      {canCancel ? (
        <button type="button" onClick={handlers.onCancel} disabled={disabled}>停止</button>
      ) : null}
    </div>
  );
};

const ExecutionDetailsBody = ({ execution }: { execution: Execution }) => (
  <>
    <p className={styles.progressSummary}>
      已完成 {execution.completed_steps}/{execution.total_steps} 步
    </p>
    {execution.steps?.length ? (
      <ol className={styles.stepList}>
        {execution.steps.map(step => (
          <li key={step.step_execution_id || step.step_id}>
            <span>{userFacingStepLabel({ capability: step.capability, step_id: step.step_id })}</span>
            <small>{runtimeStepStatusLabel(step.status)}</small>
          </li>
        ))}
      </ol>
    ) : null}
    <Link className={styles.taskCenterLink} to="/profile#my-content">
      在我的内容查看状态
      <TaskIcon width={14} height={14} aria-hidden="true" />
    </Link>
  </>
);

const ExecutionDetails = ({
  execution,
  handlers,
  disabled
}: {
  execution: Execution;
  handlers?: ExecutionHandlers;
  disabled?: boolean;
}) => {
  const [open, setOpen] = useState(false);
  return (
    <details
      className={styles.details}
      open={open}
      onToggle={event => setOpen(event.currentTarget.open)}
    >
      <summary>查看处理进度</summary>
      {open ? (
        <>
          <ExecutionDetailsBody execution={execution} />
          {handlers ? (
            <ActivityControls execution={execution} handlers={handlers} disabled={disabled ?? false} />
          ) : null}
        </>
      ) : null}
    </details>
  );
};

const RunDetails = ({
  run,
  handlers,
  disabled
}: {
  run: AgentRun;
  handlers?: ExecutionHandlers;
  disabled?: boolean;
}) => {
  const [open, setOpen] = useState(false);
  return (
    <details
      className={styles.details}
      open={open}
      onToggle={event => setOpen(event.currentTarget.open)}
    >
      <summary>查看处理进度</summary>
      {open ? (
        <>
          {run.steps.length ? (
            <ol className={styles.stepList}>
              {run.steps.map(step => (
                <li key={step.step_id}>
                  <span>{userFacingStepLabel({
                    capability: step.tool_name || step.kind,
                    step_id: step.step_id,
                    label: step.label
                  })}</span>
                  <small>{userFacingStatusLabel(step.status)}</small>
                </li>
              ))}
            </ol>
          ) : null}
          <Link className={styles.taskCenterLink} to="/profile#my-content">
            在我的内容查看状态
            <TaskIcon width={14} height={14} aria-hidden="true" />
          </Link>
          {handlers ? (
            <ActivityControls run={run} handlers={handlers} disabled={disabled ?? false} />
          ) : null}
        </>
      ) : null}
    </details>
  );
};

const ActivityCard = ({
  title,
  subtitle,
  items,
  controls,
  details
}: {
  title: string;
  subtitle?: string;
  items: AgentActivityItem[];
  controls?: ReactNode;
  details?: ReactNode;
}) => (
  <article className={styles.card}>
    <header className={styles.heading}>
      <span className={styles.activityDot} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        {subtitle ? <small>{subtitle}</small> : null}
      </div>
      {controls}
    </header>
    <ActivityList items={items} />
    {details}
  </article>
);

export const AgentExecutionActivityCard = ({
  execution,
  disabled,
  handlers,
  projectionPending = false
}: {
  execution: Execution;
  disabled: boolean;
  handlers: ExecutionHandlers;
  projectionPending?: boolean;
}) => {
  const projectedItems = projectExecutionActivity(execution);
  const items: AgentActivityItem[] = projectionPending
    ? [
        ...projectedItems.filter(item => item.status === "complete"),
        { id: "projection-sync", label: "正在整理最终结果…", status: "active" }
      ]
    : projectedItems;
  const status = String(execution.status).toUpperCase();
  const verifyingResult = String(execution.error_code || "").toUpperCase() === "RESULT_UNKNOWN";
  const title = projectionPending
    ? "正在整理结果…"
    : verifyingResult
    ? "正在确认操作结果"
    : status === "FAILED"
    ? "这次没有完成"
    : status === "CANCELLED" || execution.control_state === "CANCELLED"
      ? "已停止"
      : status === "COMPLETED"
        ? "正在整理结果…"
        : items.find(item => item.status === "active")?.label || "正在处理你的请求…";
  const subtitle = projectionPending
    ? "正在确认草稿和发布时间。"
    : verifyingResult
    ? "结果确认前请不要重复操作。"
    : status === "FAILED"
    ? "已有内容不会被本次失败覆盖。"
    : status === "COMPLETED"
      ? "结果很快会出现在对话中。"
      : "完成后会在这里显示最终结果。";

  return (
    <ActivityCard
      title={title}
      subtitle={subtitle}
      items={items}
      controls={null}
      details={<ExecutionDetails execution={execution} handlers={handlers} disabled={disabled} />}
    />
  );
};

export const AgentExecutionActivityGroup = ({
  executions,
  projectionPending = false
}: {
  executions: Execution[];
  projectionPending?: boolean;
}) => {
  const [open, setOpen] = useState(false);
  return (
    <article className={styles.card}>
      <header className={styles.heading}>
        <span className={styles.activityDot} aria-hidden="true" />
        <div>
          <strong>正在处理你的请求…</strong>
          <small>每项完成后会分别显示结果。</small>
        </div>
      </header>
      <ol className={styles.executionList}>
        {executions.map((execution, index) => {
          const projectedActivities = projectExecutionActivity(execution);
          const isTerminal = ["COMPLETED", "FAILED", "CANCELLED"].includes(String(execution.status).toUpperCase());
          const activities: AgentActivityItem[] = projectionPending && isTerminal
            ? [
                ...projectedActivities.filter(item => item.status === "complete"),
                { id: "projection-sync", label: "正在整理最终结果…", status: "active" }
              ]
            : projectedActivities;
          const item = activities.find(activity => activity.status === "active") || activities[0];
          return (
            <li key={execution.execution_id}>
              <span>{index + 1}</span>
              <div>
                <strong>{item?.label || "正在处理…"}</strong>
                <small>{item?.status === "complete" ? "已完成" : "正在处理"}</small>
              </div>
            </li>
          );
        })}
      </ol>
      <details
        className={styles.details}
        open={open}
        onToggle={event => setOpen(event.currentTarget.open)}
      >
        <summary>查看处理进度</summary>
        {open ? (
          <div className={styles.groupDetails}>
            {executions.map((execution, index) => (
              <section className={styles.groupDetailsItem} key={execution.execution_id}>
                <strong>第 {index + 1} 项</strong>
                <ExecutionDetailsBody execution={execution} />
              </section>
            ))}
          </div>
        ) : null}
      </details>
    </article>
  );
};

export const AgentRunActivityCard = ({
  run,
  disabled,
  handlers
}: {
  run: AgentRun;
  disabled: boolean;
  handlers: ExecutionHandlers;
}) => {
  const items = projectRunActivity(run);
  const title = run.status === "FAILED"
    ? "这次没有完成"
    : run.status === "CANCELLED"
      ? "已停止"
      : run.status === "PAUSED"
        ? "已暂停"
        : items.find(item => item.status === "active")?.label || "正在处理你的请求…";
  return (
    <ActivityCard
      title={title}
      subtitle={run.status === "PAUSED" ? "可以继续执行。" : "完成后会在这里显示最终结果。"}
      items={items}
      controls={null}
      details={<RunDetails run={run} handlers={handlers} disabled={disabled} />}
    />
  );
};

export const AgentApprovalCard = ({
  approval,
  disabled,
  onApprove,
  onReject,
  onModify
}: {
  approval: UserFacingApprovalRequest;
  disabled: boolean;
  onApprove: () => void;
  onReject: () => void;
  onModify: () => void;
}) => {
  return (
    <article className={styles.approvalCard} aria-label="需要你的确认">
      <header className={styles.approvalHeading}>
        <span className={styles.approvalIcon} aria-hidden="true"><ClockIcon width={18} height={18} /></span>
        <div>
          <span className={styles.kicker}>{approval.title}</span>
          <h3>{approval.actionTitle}</h3>
        </div>
      </header>
      <div className={styles.approvalBody}>
        <strong>即将执行</strong>
        <h4>{approval.resourceTitle}</h4>
        {approval.draftPreview ? <p className={styles.approvalPreview}>{approval.draftPreview}</p> : null}
        {approval.plannedTime ? <p className={styles.approvalSchedule}>计划发布时间：{approval.plannedTime}</p> : null}
        <p>{approval.description}</p>
        <small>{approval.consequence}</small>
      </div>
      {approval.canConfirm || approval.canReject || approval.canModify ? (
        <div className={styles.approvalActions}>
          {approval.canModify ? <button type="button" onClick={onModify} disabled={disabled}>返回修改</button> : null}
          {approval.canReject ? <button type="button" onClick={onReject} disabled={disabled}>拒绝</button> : null}
          {approval.canConfirm ? (
            <button type="button" className={approval.isDelete ? styles.dangerButton : styles.confirmButton} onClick={onApprove} disabled={disabled}>
              {approval.confirmLabel}
            </button>
          ) : null}
        </div>
      ) : (
        <p className={styles.approvalUnavailable} role="status">
          确认信息加载失败，但你仍然可以继续用自然语言告诉我下一步。
        </p>
      )}
    </article>
  );
};
