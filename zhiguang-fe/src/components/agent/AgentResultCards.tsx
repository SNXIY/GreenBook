import { useState } from "react";
import { Link } from "react-router-dom";
import {
  ArrowRightIcon,
  CheckIcon,
  ClockIcon,
  CloseIcon,
  TaskIcon
} from "@/components/icons/Icon";
import {
  type ChangeConfirmation,
  type ControlConfirmation,
  userFacingStatusLabel,
  userFacingStatusText,
  type UserFacingAction,
  type UserFacingInteraction,
  type UserFacingLink,
  type UserFacingResult,
  type SynthesisInteraction,
  type ResultGroupInteraction
} from "./userFacingResult";
import { formatBusinessDateTime, getDisplayTimezone } from "@/utils/dateTime";
import styles from "./AgentResultCards.module.css";

type Props = {
  interactions: UserFacingInteraction[];
  disabled?: boolean;
};

const RESULT_KICKERS: Record<UserFacingResult["type"], string> = {
  DRAFT_CREATED: "内容",
  CONTENT_REVISED: "内容",
  SCHEDULED_POST: "定时发布",
  PUBLISHED_POST: "已发布",
  SEARCH_RESULTS: "社区检索",
  SYNTHESIS_RESULT: "内容综合",
  ANALYTICS_RESULT: "内容分析",
  APPROVAL_REQUIRED: "需要确认",
  TASK_FAILED: "需要处理",
  GENERIC_RESULT: "任务结果"
};

const formatScheduleTime = (value?: string, timezone?: string) =>
  formatBusinessDateTime(value, timezone) || "发布时间待确认";

const timezoneLabel = (timezone?: string) => {
  const resolved = getDisplayTimezone(timezone);
  if (resolved === "Asia/Shanghai") return "中国标准时间";
  return resolved;
};

const ActionButton = ({
  action,
  disabled
}: {
  action: UserFacingAction;
  disabled: boolean;
}) => {
  if (action.kind !== "link" || !action.href) return null;
  const className = `${styles.action} ${
    action.tone === "primary"
      ? styles.actionPrimary
      : action.tone === "danger"
        ? styles.actionDanger
        : styles.actionSecondary
  }`;

  return (
    <Link
      className={className}
      to={action.href}
      aria-disabled={disabled || undefined}
    >
      {action.label}
      <ArrowRightIcon width={14} height={14} aria-hidden="true" />
    </Link>
  );
};

const ResultIcon = ({ failed, partial }: { failed: boolean; partial: boolean }) => (
  <span
    className={failed ? styles.resultIconFailed : partial ? styles.resultIconPartial : styles.resultIcon}
    aria-hidden="true"
  >
    {failed ? <CloseIcon width={18} height={18} /> : partial ? <ClockIcon width={18} height={18} /> : <CheckIcon width={18} height={18} />}
  </span>
);

const DraftPreview = ({ result }: { result: UserFacingResult }) => {
  if (!result.draft) return null;
  return (
    <div className={styles.resourcePreview}>
      <span className={styles.resourceLabel}>草稿</span>
      <h4>{result.draft.title}</h4>
      <p>{result.draft.preview || "草稿已经保存，可以继续编辑。"}</p>
    </div>
  );
};

const SchedulePreview = ({ result }: { result: UserFacingResult }) => {
  if (!result.schedule) return null;
  return (
    <div className={styles.schedulePreview}>
      <ClockIcon width={18} height={18} aria-hidden="true" />
      <div>
        <time dateTime={result.schedule.scheduledAt}>
          {formatScheduleTime(result.schedule.scheduledAt, result.schedule.timezone)}
        </time>
        <small>
          {userFacingStatusLabel(result.schedule.status || "SCHEDULED")} · {timezoneLabel(result.schedule.timezone)}
        </small>
      </div>
    </div>
  );
};

const SearchPreview = ({ result }: { result: UserFacingResult }) => {
  if (!result.search?.items.length) return null;
  return (
    <ol className={styles.searchList}>
      {result.search.items.slice(0, 5).map(item => (
        <li key={item.id}>
          {item.href ? (
            <Link className={styles.searchLink} to={item.href}>{item.title}</Link>
          ) : <span>{item.title}</span>}
          {item.summary ? <small>{item.summary}</small> : null}
        </li>
      ))}
    </ol>
  );
};

const AnalyticsPreview = ({ result }: { result: UserFacingResult }) => {
  if (!result.analytics) return null;
  return (
    <>
      {result.analytics.metrics.length ? (
        <dl className={styles.metricGrid}>
          {result.analytics.metrics.map(metric => (
            <div key={metric.label}>
              <dt>{metric.label}</dt>
              <dd>{typeof metric.value === "number" ? metric.value.toLocaleString("zh-CN") : metric.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {result.analytics.highlight ? (
        <p className={styles.highlight}><strong>重点</strong>{result.analytics.highlight}</p>
      ) : null}
    </>
  );
};

const SynthesisCard = ({ interaction }: { interaction: SynthesisInteraction }) => {
  const { synthesis } = interaction;
  const english = synthesis.language?.toLowerCase().startsWith("en");
  const labels = english
    ? { sources: "Key sources", common: "Common patterns", differences: "Differences", conclusion: "In summary" }
    : { sources: "重点参考", common: "共同点", differences: "差异", conclusion: "综合来看" };
  const technicalResult = {
    type: "SYNTHESIS_RESULT",
    status: interaction.status,
    title: synthesis.title,
    actions: [],
    activity: [],
    technical: interaction.technical
  } as UserFacingResult;
  return (
    <section
      className={`${styles.card} ${interaction.status === "FAILED" ? styles.cardFailed : interaction.status === "PARTIAL_SUCCESS" ? styles.cardPartial : ""}`}
      aria-label={synthesis.title}
    >
      <header className={styles.cardHeader}>
        <ResultIcon failed={interaction.status === "FAILED"} partial={interaction.status === "PARTIAL_SUCCESS"} />
        <div>
          <h3>{synthesis.title}</h3>
        </div>
      </header>

      {synthesis.intro ? <p className={styles.summary}>{synthesis.intro}</p> : null}
      {synthesis.evidenceNote ? <p className={styles.evidenceNote}>{synthesis.evidenceNote}</p> : null}

      {synthesis.sources.length ? (
        <section className={styles.synthesisSection} aria-label={labels.sources}>
          <h4>{labels.sources}</h4>
          <ol className={styles.synthesisSources}>
            {synthesis.sources.map((source, index) => (
              <li key={`${source.resourceId || source.title}-${index}`}>
                <div>
                  {source.href ? (
                    <Link className={styles.searchLink} to={source.href}>{source.title}</Link>
                  ) : <strong>{source.title}</strong>}
                  {source.readStatus === "PARTIAL" ? (
                    <small className={styles.sourceStatus}>已读取部分内容</small>
                  ) : source.readStatus === "METADATA_ONLY" ? (
                    <small className={styles.sourceStatus}>仅获取到标题，未参与综合</small>
                  ) : null}
                  {(source.excerpt || source.summary) ? <p>{source.excerpt || source.summary}</p> : null}
                </div>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {synthesis.commonPatterns.length ? (
        <section className={styles.synthesisSection} aria-label={labels.common}>
          <h4>{labels.common}</h4>
          <ol className={styles.synthesisPoints}>
            {synthesis.commonPatterns.map((point, index) => (
              <li key={`${point.title}-${index}`}>
                <strong>{point.title}</strong>
                <p>{point.explanation}</p>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {synthesis.differences?.length ? (
        <section className={styles.synthesisSection} aria-label={labels.differences}>
          <h4>{labels.differences}</h4>
          <ol className={styles.synthesisPoints}>
            {synthesis.differences.map((point, index) => (
              <li key={`${point.title}-${index}`}>
                <strong>{point.title}</strong>
                <p>{point.explanation}</p>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {synthesis.conclusion ? (
        <section className={styles.synthesisConclusion}>
          <h4>{labels.conclusion}</h4>
          <p>{synthesis.conclusion}</p>
        </section>
      ) : null}

      <NavigationLinks links={synthesis.navigation} />
    </section>
  );
};

const ExecutionDetails = ({ result }: { result: UserFacingResult }) => {
  if (!result.technical.steps.length) return null;
  const [open, setOpen] = useState(false);
  return (
    <details
      className={styles.executionDetails}
      open={open}
      onToggle={event => setOpen(event.currentTarget.open)}
    >
      <summary>查看处理进度</summary>
      {open ? (
        <>
          {result.technical.steps.length ? (
            <ol className={styles.technicalSteps}>
              {result.technical.steps.map(step => (
                <li key={step.id}>
                  <span>{step.label}</span>
                  <small>{userFacingStatusLabel(step.status)}</small>
                </li>
              ))}
            </ol>
          ) : null}
          <Link className={styles.taskCenterLink} to="/profile#my-content">
            在我的内容查看状态
            <TaskIcon width={14} height={14} aria-hidden="true" />
          </Link>
        </>
      ) : null}
    </details>
  );
};

const ResultCard = ({
  result,
  disabled
}: {
  result: UserFacingResult;
  disabled: boolean;
}) => {
  const failed = result.status === "FAILED" || result.type === "TASK_FAILED";
  const partial = result.status === "PARTIAL_SUCCESS";
  return (
    <section
      className={`${styles.card} ${failed ? styles.cardFailed : partial ? styles.cardPartial : ""}`}
      aria-label={result.title}
    >
      <header className={styles.cardHeader}>
        <ResultIcon failed={failed} partial={partial} />
        <div>
          <span className={styles.kicker}>
            {result.language?.toLowerCase().startsWith("en") && result.type === "SEARCH_RESULTS"
              ? "Search"
              : RESULT_KICKERS[result.type]}
          </span>
          <h3>{result.title}</h3>
        </div>
        <span className={`${styles.status} ${partial ? styles.statusPartial : ""}`}>
          {userFacingStatusText(result.status)}
        </span>
      </header>

      {result.summary ? <p className={styles.summary}>{result.summary}</p> : null}
      {result.hint ? <p className={styles.hint}>{result.hint}</p> : null}
      <DraftPreview result={result} />
      <SchedulePreview result={result} />
      <SearchPreview result={result} />
      <AnalyticsPreview result={result} />

      {result.actions.length ? (
        <div className={styles.actions} aria-label="结果操作">
          {result.actions.map(item => (
            <ActionButton
              key={item.id}
              action={item}
              disabled={disabled}
            />
          ))}
        </div>
      ) : null}
    </section>
  );
};

const ResultGroupCard = ({
  interaction,
  disabled
}: {
  interaction: ResultGroupInteraction;
  disabled: boolean;
}) => {
  const { group } = interaction;
  const failed = group.status === "FAILED";
  const partial = group.status === "PARTIAL_SUCCESS";
  return (
    <section
      className={
        styles.group
        + (failed ? " " + styles.cardFailed : partial ? " " + styles.cardPartial : "")
      }
      aria-label={group.title}
    >
      <header className={styles.groupHeader}>
        <div>
          <span className={styles.kicker}>本次请求</span>
          <h3>{group.title}</h3>
          {group.summary ? <p className={styles.summary}>{group.summary}</p> : null}
        </div>
        <span className={partial ? styles.statusPartial : styles.status}>
          {userFacingStatusText(group.status)}
        </span>
      </header>
      <div className={styles.groupItems}>
        {group.items.map(item => (
          <ResultCard
            key={item.id}
            result={item.result}
            disabled={disabled}
          />
        ))}
      </div>
    </section>
  );
};

const NavigationLinks = ({ links }: { links?: UserFacingLink[] }) => {
  if (!links?.length) return null;
  return (
    <div className={styles.actions} aria-label="相关页面">
      {links.map(link => (
        <Link className={`${styles.action} ${styles.actionSecondary}`} key={link.id} to={link.href}>
          {link.label}
          <ArrowRightIcon width={14} height={14} aria-hidden="true" />
        </Link>
      ))}
    </div>
  );
};

const Fact = ({ label, value }: { label: string; value?: string }) => value ? (
  <div className={styles.fact}>
    <span>{label}</span>
    <strong>{value}</strong>
  </div>
) : null;

const ChangeConfirmationCard = ({
  change,
  result
}: {
  change: ChangeConfirmation;
  result: UserFacingResult;
}) => (
  <section className={`${styles.card} ${styles.confirmationCard}`} aria-label={change.summary}>
    <header className={styles.cardHeader}>
      <ResultIcon failed={false} partial={false} />
      <div>
        <span className={styles.kicker}>已更新</span>
        <h3>{change.summary}</h3>
      </div>
    </header>
    <div className={styles.factList}>
      <Fact label="目标" value={change.targetTitle} />
      <Fact label="原来" value={change.previousValue} />
      <Fact label="现在" value={change.currentValue} />
      {change.unchangedFacts?.length ? (
        <div className={styles.fact}>
          <span>保持不变</span>
          <strong>{change.unchangedFacts.join("、")}</strong>
        </div>
      ) : null}
    </div>
    <p className={styles.hint}>{change.hint}</p>
    <NavigationLinks links={change.navigation} />
  </section>
);

const ControlConfirmationCard = ({
  control,
  result
}: {
  control: ControlConfirmation;
  result: UserFacingResult;
}) => (
  <section className={`${styles.card} ${styles.confirmationCard}`} aria-label={control.summary}>
    <header className={styles.cardHeader}>
      <ResultIcon failed={false} partial={false} />
      <div>
        <span className={styles.kicker}>状态已更新</span>
        <h3>{control.summary}</h3>
      </div>
    </header>
    <div className={styles.factList}>
      <Fact label="目标" value={control.targetTitle} />
      <Fact label="当前状态" value={control.currentStatus} />
      {control.preservedFacts?.length ? (
        <div className={styles.fact}>
          <span>仍然保留</span>
          <strong>{control.preservedFacts.join("、")}</strong>
        </div>
      ) : null}
    </div>
    <p className={styles.hint}>{control.hint}</p>
    <NavigationLinks links={control.navigation} />
  </section>
);

const InteractionCard = ({
  interaction,
  disabled
}: {
  interaction: UserFacingInteraction;
  disabled: boolean;
}) => {
  switch (interaction.kind) {
    case "RESULT_GROUP":
      return <ResultGroupCard interaction={interaction} disabled={disabled} />;
    case "SYNTHESIS_RESULT":
      return <SynthesisCard interaction={interaction} />;
    case "CHANGE_CONFIRMATION":
      return <ChangeConfirmationCard change={interaction.change} result={interaction.result} />;
    case "CONTROL_CONFIRMATION":
      return <ControlConfirmationCard control={interaction.control} result={interaction.result} />;
    case "APPROVAL_REQUEST":
    case "ASK_USER":
      return null;
    default:
      return <ResultCard result={interaction.result} disabled={disabled} />;
  }
};

export const AgentResultGroup = ({ interactions, disabled = false }: Props) => {
  if (interactions.length === 1) {
    return (
      <InteractionCard interaction={interactions[0]} disabled={disabled} />
    );
  }

  return (
    <section className={styles.group} aria-label="本次任务结果">
      <header className={styles.groupHeader}>
        <div>
          <span className={styles.kicker}>本次任务</span>
          <h3>结果已整理</h3>
        </div>
        <span>{interactions.length} 项</span>
      </header>
      <div className={styles.groupItems}>
        {interactions.map((interaction, index) => {
          const key = interaction.kind === "APPROVAL_REQUEST"
            ? interaction.approval.technical.runId
            : interaction.kind === "ASK_USER"
              ? interaction.clarification.candidates.map(item => item.identity).join("-")
              : interaction.kind === "RESULT_GROUP"
                ? interaction.group.items.map(item => item.id).join("-")
                : interaction.kind === "SYNTHESIS_RESULT"
                ? interaction.synthesis.sources.map(item => item.resourceId || item.title).join("-") || `${interaction.kind}-${index}`
                : interaction.result.technical.executionId || `${interaction.kind}-${index}`;
          return (
            <InteractionCard
              key={key}
              interaction={interaction}
              disabled={disabled}
            />
          );
        })}
      </div>
    </section>
  );
};

export default AgentResultGroup;
