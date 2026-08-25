import type { UserActivityEvent } from "@/types/userActivity";
import styles from "./UserActivityCluster.module.css";
import {
  activityLabel,
  candidateLabels,
  groupActivities
} from "./userActivityPresentation";

type Props = {
  activities: UserActivityEvent[];
  disabled?: boolean;
  resolvedApprovalActivityIds?: Readonly<
    Record<string, "APPROVED" | "REJECTED" | "COMPLETED">
  >;
  onApprovalDecision?: (
    event: UserActivityEvent,
    decision: "APPROVE" | "REJECT"
  ) => void;
};

const EMPTY_APPROVAL_ACTIVITY_IDS: Readonly<
  Record<string, "APPROVED" | "REJECTED" | "COMPLETED">
> = {};

const marker = (event: UserActivityEvent): string => {
  if (event.status === "COMPLETED") return "✓";
  if (event.status === "FAILED") return "!";
  if (event.status === "WAITING_CLARIFICATION" || event.status === "WAITING_APPROVAL") return "?";
  return "•";
};

/**
 * The ordinary user Activity surface. It deliberately never renders Run,
 * Execution, task UUID, tool, MCP, HTTP, lease, checkpoint, or raw error.
 */
const approvalIdFor = (event: UserActivityEvent): string =>
  typeof event.safe_payload.approval_id === "string"
    ? event.safe_payload.approval_id.trim()
    : "";

const payloadText = (event: UserActivityEvent, key: string): string => {
  const value = event.safe_payload[key];
  return typeof value === "string" ? value.trim() : "";
};

const UserActivityCluster = ({
  activities,
  disabled = false,
  resolvedApprovalActivityIds = EMPTY_APPROVAL_ACTIVITY_IDS,
  onApprovalDecision
}: Props) => {
  const groups = groupActivities(activities);
  if (!groups.length) return null;

  return (
    <section className={styles.cluster} aria-label="执行进度" aria-live="polite">
      <header className={styles.heading}>
        <strong>处理进度</strong>
        <small>以下状态来自已确认的执行结果</small>
      </header>
      {groups.map(group => (
        <section className={styles.task} key={group.key} aria-label={group.title}>
          <strong className={styles.taskTitle}>{group.title}</strong>
          <ol className={styles.events}>
            {group.events.map(event => {
              const candidates = candidateLabels(event);
              const preview = typeof event.safe_payload.preview === "string"
                ? event.safe_payload.preview.trim()
                : "";
              const approvalTitle = payloadText(event, "title") || "这项内容";
              const approvalDescription = payloadText(event, "description");
              const isDeleteApproval = event.activity_type === "NEEDS_APPROVAL"
                && /删除|移除|delete|remove/i.test(`${approvalDescription} ${payloadText(event, "action")}`);
              const approvalId = approvalIdFor(event);
              const resolvedApproval = resolvedApprovalActivityIds[event.activity_id];
              const canDecideApproval = Boolean(
                event.activity_type === "NEEDS_APPROVAL"
                &&
                event.status === "WAITING_APPROVAL"
                && event.run_id
                && approvalId
                && !resolvedApproval
                && onApprovalDecision
              );
              return (
                <li
                  className={styles[
                    resolvedApproval ? "statusCOMPLETED" : event.status
                  ] || styles.statusDefault}
                  key={event.activity_id}
                >
                  <span className={styles.marker} aria-hidden="true">
                    {resolvedApproval
                      ? resolvedApproval === "APPROVED" ? "✓" : "×"
                      : marker(event)}
                  </span>
                  <div>
                    {event.activity_type === "NEEDS_APPROVAL" ? (
                      resolvedApproval ? (
                        <div className={styles.approvalCard} role="status">
                          <strong>
                            {resolvedApproval === "APPROVED"
                              ? "已确认"
                              : resolvedApproval === "REJECTED" ? "已取消" : "已完成"}
                          </strong>
                          <p>
                            {resolvedApproval === "APPROVED"
                              ? "已确认执行，后续结果会继续显示在这里。"
                              : resolvedApproval === "REJECTED"
                                ? "已取消，不会继续执行这项操作。"
                                : "这项审批已经结束，不能再次操作。"}
                          </p>
                        </div>
                      ) : (
                      <div className={styles.approvalCard} role="alert">
                        <strong>需要你的确认</strong>
                        <p>
                          {isDeleteApproval
                            ? `确定删除《${approvalTitle}》吗？`
                            : approvalDescription || `确认继续处理《${approvalTitle}》吗？`}
                        </p>
                        {isDeleteApproval ? (
                          <small>确认后，这项内容将从你的社区内容中移除。</small>
                        ) : null}
                      </div>
                      )
                    ) : <span>{activityLabel(event)}</span>}
                    {preview && event.activity_type === "DRAFT_CREATED" ? (
                      <small className={styles.preview}>{preview}</small>
                    ) : null}
                    {candidates.length ? (
                      <small className={styles.candidates}>
                        可选内容：{candidates.join("、")}
                      </small>
                    ) : null}
                    {canDecideApproval ? (
                      <div className={styles.approvalActions}>
                        <button
                          type="button"
                          disabled={disabled}
                          onClick={() => onApprovalDecision?.(event, "REJECT")}
                        >
                          {isDeleteApproval ? "取消" : "暂不执行"}
                        </button>
                        <button
                          type="button"
                          className={styles.approveButton}
                          disabled={disabled}
                          onClick={() => onApprovalDecision?.(event, "APPROVE")}
                        >
                          {isDeleteApproval ? "确认删除" : "确认执行"}
                        </button>
                      </div>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      ))}
    </section>
  );
};

export default UserActivityCluster;
export { activityLabel, groupActivities } from "./userActivityPresentation";
