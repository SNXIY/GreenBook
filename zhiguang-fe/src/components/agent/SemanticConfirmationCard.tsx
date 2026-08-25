import { useEffect, useId, useState, type FormEvent } from "react";
import {
  CheckIcon,
  ClockIcon,
  CreateIcon,
  CloseIcon,
  ShieldIcon
} from "@/components/icons/Icon";
import { formatBusinessDateTime } from "@/utils/dateTime";
import type { UserActivityEvent } from "@/types/userActivity";
import {
  projectSemanticConfirmation,
  type SemanticConfirmationPayload,
  type SemanticConfirmationViewState
} from "@/types/semanticConfirmation";
import styles from "./SemanticConfirmationCard.module.css";

type Props = {
  event: UserActivityEvent;
  state?: SemanticConfirmationViewState;
  disabled?: boolean;
  error?: string | null;
  onConfirm: (event: UserActivityEvent, payload: SemanticConfirmationPayload) => void | Promise<void>;
  onCancel: (event: UserActivityEvent, payload: SemanticConfirmationPayload) => void | Promise<void>;
  onModify: (
    event: UserActivityEvent,
    payload: SemanticConfirmationPayload,
    modification: string
  ) => void | Promise<void>;
};

const statusLabel: Record<SemanticConfirmationViewState, string> = {
  WAITING_CONFIRMATION: "等待你的确认",
  CONFIRMING: "正在确认安排",
  CANCELLING: "正在取消安排",
  CONFIRMED: "已确认",
  WORKING: "已确认，正在执行",
  MODIFYING: "正在重新理解你的修改",
  CANCELLED: "已取消",
  STALE: "安排已经发生变化"
};

const outcomeFallback = (objective: SemanticConfirmationPayload["objectives"][number]): string =>
  objective.outcome || objective.desired_outcome || "按这项安排处理";

const targetLabel = (objective: SemanticConfirmationPayload["objectives"][number]): string =>
  objective.target?.label || "新建内容";

const SemanticConfirmationCard = ({
  event,
  state = "WAITING_CONFIRMATION",
  disabled = false,
  error,
  onConfirm,
  onCancel,
  onModify
}: Props) => {
  const payload = projectSemanticConfirmation(event);
  const [editing, setEditing] = useState(false);
  const [modification, setModification] = useState("");
  const inputId = useId();

  useEffect(() => {
    if (state !== "WAITING_CONFIRMATION") setEditing(state === "MODIFYING");
  }, [state]);

  if (!payload) return null;

  const isWaiting = state === "WAITING_CONFIRMATION";
  const isModifying = state === "MODIFYING";
  const canEdit = isWaiting || isModifying;
  const submitModification = (eventValue: FormEvent<HTMLFormElement>) => {
    eventValue.preventDefault();
    const value = modification.trim();
    if (!value || disabled) return;
    void onModify(event, payload, value);
  };

  return (
    <article className={styles.card} aria-labelledby={`${inputId}-title`}>
      <header className={styles.header}>
        <span className={styles.iconBadge} aria-hidden="true">
          <ShieldIcon width={20} height={20} />
        </span>
        <div className={styles.headingCopy}>
          <span className={styles.eyebrow}>请先确认理解</span>
          <h3 id={`${inputId}-title`}>{payload.title}</h3>
          <p>我已经把这次安排整理成下面的目标，确认后才会开始执行。</p>
        </div>
        <span className={`${styles.state} ${styles[`state${state}`]}`} aria-live="polite">
          {stateLabel(state)}
        </span>
      </header>

      <div className={styles.objectives}>
        {payload.objectives.map((objective, index) => (
          <section className={styles.objective} key={`${objective.topic}-${index}`}>
            <div className={styles.objectiveNumber} aria-hidden="true">{index + 1}</div>
            <div className={styles.objectiveBody}>
              <h4>{objective.topic || objective.desired_outcome || `第 ${index + 1} 项目标`}</h4>
              <dl className={styles.facts}>
                <div>
                  <dt>要做什么</dt>
                  <dd>{objective.desired_outcome || "按已整理的内容完成"}</dd>
                </div>
                <div>
                  <dt>最终结果</dt>
                  <dd>{outcomeFallback(objective)}</dd>
                </div>
                {objective.target?.label ? (
                  <div>
                    <dt>目标</dt>
                    <dd>{targetLabel(objective)}</dd>
                  </div>
                ) : null}
                {objective.run_at ? (
                  <div>
                    <dt><ClockIcon width={14} height={14} aria-hidden="true" /> 发布时间</dt>
                    <dd>
                      {formatBusinessDateTime(objective.run_at, objective.timezone) || objective.run_at}
                      {objective.timezone ? <span className={styles.timezone}> · {objective.timezone}</span> : null}
                    </dd>
                  </div>
                ) : null}
                {objective.dependencies.length ? (
                  <div>
                    <dt>前置安排</dt>
                    <dd>{objective.dependencies.join("、")}</dd>
                  </div>
                ) : null}
              </dl>
              {objective.has_real_side_effect ? (
                <p className={styles.sideEffect}>
                  <ShieldIcon width={14} height={14} aria-hidden="true" />
                  确认后会产生真实内容或发布变化
                </p>
              ) : null}
            </div>
          </section>
        ))}
      </div>

      {error ? <p className={styles.error} role="alert">{error}</p> : null}

      {canEdit && editing ? (
        <form className={styles.modifyForm} onSubmit={submitModification}>
          <label htmlFor={inputId}>告诉我需要怎样调整</label>
          <textarea
            id={inputId}
            value={modification}
            onChange={eventValue => setModification(eventValue.target.value)}
            placeholder="例如：把第二篇改为明天早上 9 点发布"
            disabled={disabled}
            rows={3}
          />
          <div className={styles.modifyHint}>旧安排会先失效，新的理解需要重新确认。</div>
          <div className={styles.modifyActions}>
            {isWaiting ? (
              <button
                className={styles.secondaryButton}
                type="button"
                disabled={disabled}
                onClick={() => setEditing(false)}
              >
                收起
              </button>
            ) : null}
            {isWaiting ? (
              <button
                className={styles.cancelButton}
                type="button"
                disabled={disabled}
                onClick={() => void onCancel(event, payload)}
              >
                <CloseIcon width={16} height={16} aria-hidden="true" />
                取消
              </button>
            ) : null}
            <button className={styles.primaryButton} type="submit" disabled={disabled || !modification.trim()}>
              <CreateIcon width={16} height={16} aria-hidden="true" />
              {isModifying ? "重新提交修改" : "提交修改"}
            </button>
          </div>
        </form>
      ) : null}

      {isWaiting && !editing ? (
        <div className={styles.actions}>
          <button
            className={styles.primaryButton}
            type="button"
            disabled={disabled}
            onClick={() => void onConfirm(event, payload)}
          >
            <CheckIcon width={16} height={16} aria-hidden="true" />
            确认执行
          </button>
          <button
            className={styles.secondaryButton}
            type="button"
            disabled={disabled}
            onClick={() => setEditing(true)}
          >
            <CreateIcon width={16} height={16} aria-hidden="true" />
            修改安排
          </button>
          <button
            className={styles.cancelButton}
            type="button"
            disabled={disabled}
            onClick={() => void onCancel(event, payload)}
          >
            <CloseIcon width={16} height={16} aria-hidden="true" />
            取消
          </button>
        </div>
      ) : null}

      {state === "CONFIRMING" ? <div className={styles.progress} role="status">确认已提交，正在恢复原任务…</div> : null}
      {state === "CANCELLING" ? <div className={styles.progress} role="status">正在取消，这项安排不会执行…</div> : null}
      {state === "CONFIRMED" ? <div className={styles.success} role="status"><CheckIcon width={16} height={16} aria-hidden="true" />已确认，等待任务开始</div> : null}
      {state === "WORKING" ? <div className={styles.success} role="status"><span className={styles.pulse} aria-hidden="true" />已确认，正在执行原任务</div> : null}
      {state === "CANCELLED" ? <div className={styles.cancelled} role="status"><CloseIcon width={16} height={16} aria-hidden="true" />这项安排已取消，不会执行</div> : null}
      {state === "STALE" ? <div className={styles.stale} role="status">请刷新对话，以最新安排为准。</div> : null}
    </article>
  );
};

const stateLabel = (state: SemanticConfirmationViewState): string => statusLabel[state];

export default SemanticConfirmationCard;
