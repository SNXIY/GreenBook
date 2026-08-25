import type {
  AgentMessage,
  AgentExecutionResultPart,
  AgentResultArtifact,
  AgentRun,
  AgentRunStep,
  BusinessProjection,
  BusinessState,
  AgentTargetClarificationPart,
  AgentUserFacingInteractionPart
} from "@/types/agent";
import type { Execution } from "@/types/execution";
import { formatBusinessDateTime, getDisplayTimezone } from "@/utils/dateTime";

/**
 * Polling may return several projections for the same failed Goal while an
 * older continuation is being drained.  A terminal card is identified by
 * durable Task + Goal identity, never by its user-facing text.
 */
export const dedupeTerminalAgentMessages = (
  messages: AgentMessage[]
): AgentMessage[] => {
  const seen = new Set<string>();
  return messages.filter(message => {
    const terminalKeys = (message.parts ?? [])
      .filter((part): part is AgentExecutionResultPart => part.type === "execution_result")
      .filter(part => ["FAILED", "CANCELLED"].includes(part.execution.status.toUpperCase()))
      .map(part => {
        const failedStep = (part.execution.steps ?? []).find(step =>
          ["FAILED", "CANCELLED"].includes(String(step.status ?? "").toUpperCase())
        );
        const taskId = String(part.execution.task_id ?? message.run_id ?? "");
        const goalId = String(failedStep?.goal_id ?? failedStep?.capability ?? "terminal");
        return taskId ? `${taskId}:${goalId}:TERMINAL` : "";
      })
      .filter(Boolean);
    if (!terminalKeys.length) return true;
    if (terminalKeys.every(key => seen.has(key))) return false;
    terminalKeys.forEach(key => seen.add(key));
    return true;
  });
};

/**
 * A clarification is actionable only until the conversation has continued.
 * The API keeps the original assistant message as history after the user
 * selects a candidate, so rendering every target_clarification part forever
 * would resurrect its buttons after the operation has already completed.
 */
export const isTargetClarificationResolved = (
  messages: AgentMessage[],
  messageIndex: number
): boolean => messages
  .slice(messageIndex + 1)
  .some(message => message.role === "user" || message.role === "assistant");

export type UserFacingResultType =
  | "DRAFT_CREATED"
  | "CONTENT_REVISED"
  | "SCHEDULED_POST"
  | "PUBLISHED_POST"
  | "SEARCH_RESULTS"
  | "SYNTHESIS_RESULT"
  | "ANALYTICS_RESULT"
  | "APPROVAL_REQUIRED"
  | "TASK_FAILED"
  | "GENERIC_RESULT";

export type UserFacingStatus =
  | "SUCCESS"
  | "PARTIAL_SUCCESS"
  | "NEEDS_ACTION"
  | "FAILED"
  | "IN_PROGRESS";

export type UserFacingAction = {
  id: string;
  label: string;
  kind: "link";
  href: string;
  tone?: "primary" | "secondary" | "danger";
};

export type UserFacingLink = {
  id: string;
  label: string;
  href: string;
};

export type ChangeConfirmation = {
  targetTitle?: string;
  changeType: "CONTENT" | "TITLE" | "SCHEDULE" | "STATUS" | "OTHER";
  summary: string;
  previousValue?: string;
  currentValue?: string;
  unchangedFacts?: string[];
  navigation?: UserFacingLink[];
  hint: string;
};

export type ControlConfirmation = {
  targetTitle?: string;
  changeType: "PAUSE" | "RESUME" | "CANCEL" | "SCHEDULE" | "STATUS" | "OTHER";
  summary: string;
  currentStatus?: string;
  preservedFacts?: string[];
  navigation?: UserFacingLink[];
  hint: string;
};

export type UserFacingApprovalRequest = {
  title: string;
  actionTitle: string;
  resourceTitle: string;
  draftPreview?: string;
  plannedTime?: string;
  resourceId?: string;
  description: string;
  consequence: string;
  confirmLabel: string;
  isDelete: boolean;
  canConfirm: boolean;
  canReject: boolean;
  canModify: boolean;
  technical: {
    runId: string;
    approvalId?: string;
    action?: string;
    resourceId?: string;
  };
};

export type UserFacingTargetCandidate = {
  identity: string;
  type: string;
  label: string;
  status: string;
};

export type UserFacingTargetClarification = {
  question: string;
  description: string;
  candidates: UserFacingTargetCandidate[];
};

export type AgentActivityStatus = "complete" | "active" | "pending" | "failed";

export type AgentActivityItem = {
  id: string;
  label: string;
  status: AgentActivityStatus;
};

export type UserFacingTechnicalStep = {
  id: string;
  label: string;
  status: string;
  error?: string | null;
};

export type UserFacingResult = {
  type: UserFacingResultType;
  status: UserFacingStatus;
  title: string;
  language?: "zh" | "en" | string;
  summary?: string;
  resourceId?: string;
  draft?: {
    draftId: string;
    title: string;
    preview?: string;
  };
  schedule?: {
    scheduleId: string;
    draftId?: string;
    scheduledAt?: string;
    timezone?: string;
    status?: string;
  };
  post?: {
    postId: string;
    title?: string;
  };
  search?: {
    count?: number;
    items: Array<{ id: string; title: string; summary?: string; href?: string }>;
  };
  analytics?: {
    metrics: Array<{ label: string; value: string | number }>;
    highlight?: string;
  };
  actions: UserFacingAction[];
  hint?: string;
  activity: AgentActivityItem[];
  businessState?: BusinessState;
  businessProjection?: BusinessProjection;
  technical: {
    executionId: string;
    taskId?: string;
    status: string;
    steps: UserFacingTechnicalStep[];
    errorCode?: string;
  };
};

export type SynthesisSourceItem = {
  resourceId?: string;
  title: string;
  excerpt?: string;
  summary?: string;
  href?: string;
  readStatus?: "FULL" | "PARTIAL" | "METADATA_ONLY";
  sourceRefs?: string[];
};

export type SynthesisPoint = {
  title: string;
  explanation: string;
  sourceRefs?: string[];
};

export type SynthesisResult = {
  title: string;
  language?: "zh" | "en" | string;
  intro?: string;
  totalMatched?: number;
  selectedCount?: number;
  readCount?: number;
  failedCount?: number;
  sources: SynthesisSourceItem[];
  commonPatterns: SynthesisPoint[];
  differences?: SynthesisPoint[];
  conclusion?: string;
  evidenceNote?: string;
  navigation?: UserFacingLink[];
};

export type SynthesisInteraction = {
  kind: "SYNTHESIS_RESULT";
  synthesis: SynthesisResult;
  status: UserFacingStatus;
  technical: UserFacingResult["technical"];
};

export type UserFacingGoalResult = {
  id: string;
  result: UserFacingResult;
};

export type UserFacingResultGroup = {
  title: string;
  summary?: string;
  status: UserFacingStatus;
  items: UserFacingGoalResult[];
};

export type ResultGroupInteraction = {
  kind: "RESULT_GROUP";
  group: UserFacingResultGroup;
};

export type UserFacingInteraction =
  | ResultGroupInteraction
  | { kind: "CONTENT_RESULT"; result: UserFacingResult }
  | { kind: "QUERY_RESULT"; result: UserFacingResult }
  | SynthesisInteraction
  | { kind: "ANALYSIS_RESULT"; result: UserFacingResult }
  | { kind: "CHANGE_CONFIRMATION"; result: UserFacingResult; change: ChangeConfirmation }
  | { kind: "CONTROL_CONFIRMATION"; result: UserFacingResult; control: ControlConfirmation }
  | { kind: "FAILURE_RESULT"; result: UserFacingResult }
  | { kind: "APPROVAL_REQUEST"; approval: UserFacingApprovalRequest }
  | { kind: "ASK_USER"; clarification: UserFacingTargetClarification };

const STEP_COPY: Record<string, { active: string; complete: string; failed?: string }> = {
  SEARCH_COMMUNITY: { active: "正在查找相关内容…", complete: "已查找相关内容", failed: "未能完成查询" },
  GET_POST_DETAIL: { active: "正在阅读代表性内容…", complete: "已阅读代表性内容", failed: "部分内容未能读取" },
  READ_POST: { active: "正在阅读代表性内容…", complete: "已阅读代表性内容", failed: "部分内容未能读取" },
  SYNTHESIZE_RESULTS: { active: "正在整理共同观点…", complete: "已整理共同观点", failed: "未能完成综合" },
  SYNTHESIZE_CONTENT: { active: "正在整理共同观点…", complete: "已整理共同观点", failed: "未能完成综合" },
  CROSS_DOCUMENT_SYNTHESIS: { active: "正在比较这些内容…", complete: "已比较这些内容", failed: "未能完成综合" },
  ANALYZE_CONTENT_PATTERNS: { active: "正在分析相关内容…", complete: "已分析相关内容", failed: "未能完成分析" },
  GENERATE_CONTENT: { active: "正在生成文章…", complete: "内容已生成", failed: "内容生成未完成" },
  IMPROVE_CONTENT: { active: "正在修改文章…", complete: "内容已更新", failed: "内容修改未完成" },
  REVISE_DRAFT: { active: "正在修改文章…", complete: "内容已更新", failed: "内容修改未完成" },
  VALIDATE_QUALITY: { active: "正在检查内容…", complete: "内容已检查", failed: "内容检查未完成" },
  SCHEDULE_PUBLISH: { active: "正在安排发布时间…", complete: "已安排发布时间", failed: "未能安排发布时间" },
  MANAGE_SCHEDULE: { active: "正在调整发布时间…", complete: "发布时间已调整", failed: "未能调整发布时间" },
  CANCEL_SCHEDULE: { active: "正在取消发布计划…", complete: "发布计划已取消", failed: "未能取消发布计划" },
  PUBLISH_NOW: { active: "正在发布内容…", complete: "内容已发布", failed: "内容发布未完成" }
};

/** Business activity copy for a semantic capability ("正在生成内容…"). */
/** LEGACY_FALLBACK: new UserActivityEvent payloads carry display_key instead. */
export const capabilityActiveLabel = (capability: string): string | null => {
  const normalized = upper(text(capability)).replace(/-/g, "_");
  return STEP_COPY[normalized]?.active || null;
};

const STATUS_LABELS: Record<string, string> = {
  COMPLETED: "已完成",
  SCHEDULED: "已安排发布",
  DRAFT: "草稿已保存",
  READY: "草稿已保存",
  PUBLISHED: "已发布",
  FAILED: "需要处理",
  CANCELLED: "已取消",
  RUNNING: "进行中",
  QUEUED: "即将开始",
  WAITING_APPROVAL: "等待确认",
  WAITING_HUMAN: "等待你的处理",
  PAUSED: "已暂停"
};

const text = (value: unknown): string =>
  typeof value === "string" ? value.trim() : value == null ? "" : String(value).trim();

const INTERNAL_RUNTIME_COPY = /durable\s+queue|executioninput|taskplan|toolruntime|stepexecution|\bruntime\b|\bexecution\b|\bmcp\b|RESULT_UNKNOWN|WAITING_EXTERNAL|TARGET_RESOLUTION_AMBIGUOUS|TEMPORAL_NOT_RESOLVED|TOOL_ARGUMENT_VALIDATION_FAILED|(?:execution|operation|objective|task|run)_id/i;
const INTERNAL_PROMPT_COPY = /generate\s+a\s+draft\s+post|overall\s+user\s+objective\s+context|preserve\s+its\s+topic|execute\s+only\s+this\s+content-generation\s+step|planner\s+instruction|compiler\s+instruction|creator\s+instruction|system\s+prompt|tool\s+arguments|raw\s+prompt|internal\s+objective/i;
const INTERNAL_REFLECTION_COPY = /the user's goal\s+is\s+satisfied|the task\s+(was\s+)?successfully completed|the task has been completed|the search(?: for [^\n]+)? returned\s+\d+\s+results|detailed content was retrieved|analysis of these posts reveals|goal completion|execution successfully retrieved|the agent decided|the observation is sufficient|the task is complete/i;
const INTERNAL_IDENTIFIER = /^(?:g\d+:\d+|(?:execution|task|plan|goal|run|step)[\w:-]+)$/i;
const INTERNAL_SOURCE_REFERENCE = /\b(?:source|evidence|artifact)[-_][A-Za-z0-9:_-]+\b|\b(?:execution|task|plan|goal|run|step)[-_][A-Za-z0-9:_-]+\b|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b/gi;
const UNVERIFIED_HEDGE = /\b(?:may|might|possibly|probably|perhaps|seems|appears)\b|(?:可能|大概|应该|似乎|或许)/i;
const containsInternalSourceReference = (value: string): boolean =>
  new RegExp(INTERNAL_SOURCE_REFERENCE.source, "i").test(value);

export const userFacingMessage = (value: string): string => {
  if (
    !INTERNAL_RUNTIME_COPY.test(value)
    && !INTERNAL_PROMPT_COPY.test(value)
    && !INTERNAL_REFLECTION_COPY.test(value)
    && !INTERNAL_IDENTIFIER.test(value.trim())
  ) return value;
  const normalized = value.toUpperCase();
  if (/RESULT_UNKNOWN|WAITING_EXTERNAL/.test(normalized)) {
    return "正在确认操作结果，请稍候。";
  }
  if (/TARGET_RESOLUTION_AMBIGUOUS/.test(normalized)) {
    return "我找到多个可能的内容，请选择你要操作的那一篇。";
  }
  if (/TEMPORAL_NOT_RESOLVED/.test(normalized)) {
    return "你想安排在什么时候发布？";
  }
  if (/FAILED|FAILURE|ERROR|REJECTED|UNAVAILABLE/.test(normalized)) {
    return "这次没有完成，你可以稍后重试。";
  }
  if (/WAITING_APPROVAL|WAITING_HUMAN|APPROVAL/.test(normalized)) {
    return "需要你的确认，具体操作请看上方卡片。";
  }
  return "已收到请求，正在准备内容…";
};

const userFacingText = (value: unknown, fallback = ""): string => {
  const normalized = text(value);
  if (
    !normalized
    || INTERNAL_RUNTIME_COPY.test(normalized)
    || INTERNAL_PROMPT_COPY.test(normalized)
    || INTERNAL_REFLECTION_COPY.test(normalized)
    || INTERNAL_IDENTIFIER.test(normalized)
  ) {
    return fallback;
  }
  return normalized;
};

const userFacingProse = (value: unknown, fallback = ""): string => {
  const normalized = userFacingText(value);
  if (!normalized) return fallback;
  const cleaned = normalized
    .replace(INTERNAL_SOURCE_REFERENCE, "")
    .replace(/\s{2,}/g, " ")
    .replace(/\s+([，。！？,.!?])/g, "$1")
    .trim();
  return cleaned || fallback;
};

export const userFacingDisplayText = userFacingText;

const compact = (value: unknown, limit = 180): string => {
  const normalized = text(value).replace(/\s+/g, " ");
  return normalized.length > limit
    ? `${normalized.slice(0, Math.max(0, limit - 1)).trimEnd()}…`
    : normalized;
};

const cleanSourceExcerpt = (value: unknown): string => {
  const normalized = userFacingProse(value)
    .replace(/```(?:markdown|md|text)?/gi, "")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .split(/\r?\n/)
    .map(line => line
      .replace(/^\s{0,3}#{1,6}\s*/, "")
      .replace(/^\s*[-*+]\s+/, "")
      .replace(/^\s*\d+[.)]\s+/, "")
      .trim()
    )
    .filter(Boolean)
    .join(" ");
  return compact(normalized, 260);
};

const upper = (value: unknown): string => text(value).toUpperCase();

const artifactKind = (artifact: AgentResultArtifact): string =>
  upper(artifact.resource_type || artifact.type);

const artifactPayload = (artifact?: AgentResultArtifact): Record<string, unknown> =>
  artifact?.payload && typeof artifact.payload === "object" ? artifact.payload : {};

const artifactResourceId = (artifact?: AgentResultArtifact): string => {
  if (!artifact) return "";
  const payload = artifactPayload(artifact);
  return text(
    artifact.resource_id
      || payload.draft_id
      || payload.schedule_id
      || payload.post_id
      || payload.id
  );
};

const artifactDraftId = (artifact?: AgentResultArtifact): string => {
  if (!artifact) return "";
  const payload = artifactPayload(artifact);
  return text(artifact.draft_id || payload.draft_id || payload.draftId);
};

const artifactStepId = (artifact?: AgentResultArtifact): string =>
  text(artifact?.step_id || artifactPayload(artifact).step_id);

const artifactGoalId = (artifact?: AgentResultArtifact): string =>
  text(artifact?.goal_id || artifactPayload(artifact).goal_id || artifactPayload(artifact).goalId);

const stepScopeValue = (stepId: string): string =>
  stepId.includes(":") ? stepId.split(":")[0] : stepId;

const stepGoalId = (step?: ProjectionStep): string => text(step?.goal_id);

const findArtifact = (
  artifacts: AgentResultArtifact[],
  kinds: string[]
): AgentResultArtifact | undefined => {
  const wanted = new Set(kinds.map(upper));
  return artifacts.find(artifact => {
    const kind = artifactKind(artifact);
    const type = upper(artifact.type);
    return wanted.has(kind) || wanted.has(type);
  });
};

const stepKey = (step: { step_id?: string; label?: string; capability?: string }): string =>
  upper(step.capability || step.step_id || step.label);

const semanticStepKey = (
  step: { step_id?: string; label?: string; capability?: string },
  retrievalContext = false
): string => {
  const key = stepKey(step);
  if (/SEARCH|FIND|LIST_OWN_POSTS|COMMUNITY/.test(key) && !/DETAIL|GET_POST|READ/.test(key)) {
    return "SEARCH_COMMUNITY";
  }
  if (/GET_POST|POST_DETAIL|READ_POST|READ_SOURCE|DETAIL/.test(key)) {
    return "GET_POST_DETAIL";
  }
  if (/SYNTH|CROSS_DOCUMENT/.test(key)) return "SYNTHESIZE_RESULTS";
  if (retrievalContext && /ANALYZE_CONTENT_PATTERNS/.test(key)) return "SYNTHESIZE_RESULTS";
  return key;
};

const stepLabel = (
  step: { step_id?: string; label?: string; capability?: string },
  retrievalContext = false
): string => {
  const copy = STEP_COPY[semanticStepKey(step, retrievalContext)];
  if (copy) return copy.active.replace(/…$/, "");
  for (const candidate of [step.label, step.capability, step.step_id]) {
    const raw = userFacingText(candidate);
    if (raw && /[\s\u3400-\u9fff]/.test(raw) && !/[.:/]/.test(raw)) return raw;
  }
  return "正在处理你的请求";
};

export const userFacingStepLabel = stepLabel;

const stepActivity = (
  steps: Array<{ step_id?: string; label?: string; capability?: string; status?: string; error?: string | null }>
): AgentActivityItem[] => {
  const rawKeys = steps.map(step => stepKey(step));
  const retrievalContext = rawKeys.some(key => /SEARCH|FIND|LIST_OWN_POSTS|COMMUNITY/.test(key))
    && rawKeys.some(key => /GET_POST|POST_DETAIL|READ_POST|READ_SOURCE|DETAIL/.test(key));
  return steps
    .filter(step => upper(step.status) !== "SKIPPED")
    .slice(0, 5)
    .map((step, index) => {
    const status = upper(step.status);
    const copy = STEP_COPY[semanticStepKey(step, retrievalContext)];
    return {
      id: text(step.step_id) || `${semanticStepKey(step, retrievalContext)}-${index}`,
      label: status === "COMPLETED"
        ? copy?.complete || `${stepLabel(step, retrievalContext)}已完成`
        : status === "FAILED" || status === "FAILED_RETRYABLE"
          ? copy?.failed || `${stepLabel(step, retrievalContext)}未完成`
          : copy?.active || `${stepLabel(step, retrievalContext)}…`,
      status: status === "COMPLETED"
        ? "complete"
        : status === "FAILED" || status === "FAILED_RETRYABLE"
          ? "failed"
          : status === "RUNNING" || status === "WAITING_APPROVAL" || status === "WAITING_HUMAN"
            ? "active"
            : "pending"
    };
    });
};

const technicalSteps = (
  steps: Array<{ step_id?: string; label?: string; capability?: string; status?: string; error?: string | null }>
): UserFacingTechnicalStep[] => steps.map((step, index) => ({
  id: text(step.step_id) || `${stepKey(step)}-${index}`,
  label: userFacingText(step.label || step.capability || step.step_id, "步骤"),
  status: upper(step.status) || "PENDING",
  error: step.error || null
}));

const makeTechnical = (part: AgentExecutionResultPart): UserFacingResult["technical"] => ({
  executionId: part.execution.execution_id,
  taskId: text(part.execution.task_id) || undefined,
  status: upper(part.execution.status),
  steps: technicalSteps(part.execution.steps || [])
});

const action = (
  id: string,
  label: string,
  href: string,
  tone: UserFacingAction["tone"] = "secondary"
): UserFacingAction => ({ id, label, kind: "link", href, tone });

const toSearchItems = (artifact: AgentResultArtifact | undefined) => {
  if (!artifact) return [];
  const payload = artifactPayload(artifact);
  const values = [payload.items, payload.results, payload.posts].find(Array.isArray);
  if (!Array.isArray(values)) return [];
  return values
    .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === "object")
    .map((item, index) => {
      const itemId = text(item.id);
      const resourceId = text(item.post_id || item.postId || itemId);
      const postId = text(item.post_id || item.postId);
      return {
      id: resourceId || `result-${index}`,
      title: userFacingText(item.title || item.name, "未命名内容"),
      summary: compact(userFacingText(item.summary || item.description), 100) || undefined,
      href: postId ? `/post/${encodeURIComponent(postId)}` : undefined
      };
    });
};

const toAnalytics = (artifact: AgentResultArtifact | undefined) => {
  if (!artifact) return { metrics: [], highlight: undefined };
  const payload = artifactPayload(artifact);
  const aliases: Array<[string[], string]> = [
    [["totalPublished", "total_published", "published"], "发布"],
    [["viewCount", "view_count", "views"], "浏览"],
    [["likeCount", "like_count", "likes", "totalLikesReceived"], "点赞"],
    [["commentCount", "comment_count", "comments", "totalComments"], "评论"],
    [["favoriteCount", "favorite_count", "favorites", "totalFavorites"], "收藏"],
    [["shareCount", "share_count", "shares"], "分享"],
    [["followerCount", "follower_count", "followers"], "粉丝"],
    [["followingCount", "following_count", "following"], "关注"]
  ];
  const metrics = aliases.flatMap(([keys, label]) => {
    const key = keys.find(candidate => payload[candidate] !== undefined && payload[candidate] !== null);
    if (!key) return [];
    const value = payload[key];
    if (typeof value !== "number" && typeof value !== "string") return [];
    return [{ label, value }];
  });
  const highlight = compact(userFacingText(
    payload.highlight || payload.top_post_title || payload.topPostTitle
  ), 100) || undefined;
  return { metrics, highlight };
};

const partStepKeys = (part: AgentExecutionResultPart): string[] =>
  (part.execution.steps || []).map(step => stepKey(step));

const changeStepKey = (part: AgentExecutionResultPart): string =>
  partStepKeys(part).find(key =>
    /IMPROVE|REVISE|MODIF|UPDATE_TITLE|EDIT_CONTENT|UPDATE_CONTENT|CONTENT_UPDATE|CHANGE_TITLE|MANAGE_SCHEDULE|UPDATE_SCHEDULE|RESCHEDULE/.test(key)
  ) || "";

const controlStepKey = (part: AgentExecutionResultPart): string =>
  partStepKeys(part).find(key =>
    /^(CANCEL_SCHEDULE|PAUSE|RESUME|INTERRUPT|CANCEL|CONTROL)/.test(key)
  ) || "";

const hasRevisionStep = (part: AgentExecutionResultPart): boolean => Boolean(changeStepKey(part));

const statusLabel = (status: string): string => STATUS_LABELS[upper(status)] || "状态已更新";

export const userFacingStatusLabel = statusLabel;

const targetTypeLabel = (type: string): string => ({
  TASK: "任务",
  DRAFT: "草稿",
  SCHEDULE: "定时发布",
  POST: "帖子",
  EXECUTION: "任务记录",
  APPROVAL: "待确认操作"
}[upper(type)] || "内容");

export const userFacingStatusText = (status: UserFacingStatus): string => ({
  SUCCESS: "已完成",
  IN_PROGRESS: "进行中",
  PARTIAL_SUCCESS: "部分完成",
  NEEDS_ACTION: "需要确认",
  FAILED: "需要处理"
}[status]);

export const userFacingStatusFromBusinessProjection = (
  projection?: BusinessProjection | null
): UserFacingStatus | undefined => {
  if (!projection || projection.visible === false || !projection.state) return undefined;
  switch (projection.state) {
    case "NEEDS_CONFIRMATION":
    case "NEEDS_APPROVAL":
      return "NEEDS_ACTION";
    case "PROCESSING":
    case "VERIFYING_RESULT":
      return "IN_PROGRESS";
    case "FAILED":
      return "FAILED";
    case "PARTIAL":
      return "PARTIAL_SUCCESS";
    default:
      return "SUCCESS";
  }
};

const failedStepKey = (part: AgentExecutionResultPart): string => {
  const failedStep = (part.execution.steps || []).find(step =>
    ["FAILED", "FAILED_RETRYABLE"].includes(upper(step.status))
  );
  return failedStep ? stepKey(failedStep) : "";
};

const failureActions = (
  draftId: string
): UserFacingAction[] => {
  if (!draftId) return [];
  return [action(
    "view-draft",
    "查看草稿",
    `/create/manual?draftId=${encodeURIComponent(draftId)}`
  )];
};

type ResultProjectionOptions = {
  artifacts?: AgentResultArtifact[];
  schedule?: Record<string, unknown> | null;
  steps?: AgentExecutionResultPart["execution"]["steps"];
  executionStatus?: string;
};

type ProjectionStep = NonNullable<AgentExecutionResultPart["execution"]["steps"]>[number];

type ArtifactGroup = {
  id: string;
  goalId?: string;
  artifacts: AgentResultArtifact[];
  stepIds: Set<string>;
  stepScopes: Set<string>;
};

const artifactRelationValue = (
  artifact: AgentResultArtifact,
  ...keys: string[]
): string => {
  const raw = artifact as unknown as Record<string, unknown>;
  const payload = artifactPayload(artifact);
  for (const key of keys) {
    const value = raw[key] ?? payload[key];
    if (value !== undefined && value !== null && text(value)) return text(value);
  }
  return "";
};

const stepScope = (artifact?: AgentResultArtifact): string => {
  const stepId = artifactStepId(artifact);
  return stepId.includes(":") ? stepId.split(":")[0] : stepId;
};

const groupRelationKeys = (group: ArtifactGroup): string[] =>
  [
    group.goalId ? "goal:" + group.goalId : "",
    ...Array.from(group.stepIds).flatMap(stepId => [
      "step:" + stepId,
      stepScopeValue(stepId) ? "step:" + stepScopeValue(stepId) : ""
    ]),
    ...Array.from(group.stepScopes).map(scope => "step:" + scope),
    ...group.artifacts.flatMap(artifact => [
      (artifactDraftId(artifact)
        || (["DRAFT", "POST_DRAFT", "CONTENT_DRAFT"].includes(artifactKind(artifact))
          ? artifactResourceId(artifact)
          : ""))
        ? "draft:" + (artifactDraftId(artifact) || artifactResourceId(artifact))
        : "",
      artifactRelationValue(artifact, "task_id", "taskId")
        ? "task:" + artifactRelationValue(artifact, "task_id", "taskId")
        : "",
      artifactRelationValue(artifact, "goal_id", "goalId")
        ? "goal:" + artifactRelationValue(artifact, "goal_id", "goalId")
        : "",
      artifactStepId(artifact) ? "step:" + artifactStepId(artifact) : "",
      stepScope(artifact) ? "step:" + stepScope(artifact) : ""
    ])
  ].flat().filter(Boolean);

const attachableGroup = (
  groups: ArtifactGroup[],
  artifact: AgentResultArtifact
): ArtifactGroup | undefined => {
  const draftId = artifactDraftId(artifact);
  if (draftId) {
    const exact = groups.find(group => groupRelationKeys(group).includes("draft:" + draftId));
    if (exact) return exact;
  }

  const relationKeys = [
    artifactRelationValue(artifact, "task_id", "taskId")
      ? "task:" + artifactRelationValue(artifact, "task_id", "taskId")
      : "",
    artifactRelationValue(artifact, "goal_id", "goalId")
      ? "goal:" + artifactRelationValue(artifact, "goal_id", "goalId")
      : "",
    artifactStepId(artifact) ? "step:" + artifactStepId(artifact) : "",
    stepScope(artifact) ? "step:" + stepScope(artifact) : ""
  ].filter(Boolean);
  const matches = groups.filter(group =>
    relationKeys.some(key => groupRelationKeys(group).includes(key))
  );
  return matches.length === 1 ? matches[0] : undefined;
};

const businessArtifact = (artifact: AgentResultArtifact): boolean =>
  [
    "DRAFT", "POST_DRAFT", "CONTENT_DRAFT", "SCHEDULE",
    "PUBLICATION_SCHEDULE", "POST", "PUBLISHED_POST", "PUBLICATION",
    "SEARCH_RESULT", "SEARCH_RESULTS", "ANALYSIS_REPORT", "PERFORMANCE_DATA",
    "SYNTHESIS_RESULT", "CONTENT_SYNTHESIS"
  ].includes(artifactKind(artifact));

const emptyGroup = (id: string, goalId?: string): ArtifactGroup => ({
  id,
  goalId,
  artifacts: [],
  stepIds: new Set<string>(),
  stepScopes: new Set<string>()
});

const addStepToGroup = (group: ArtifactGroup, step: ProjectionStep): void => {
  const stepId = text(step.step_id);
  if (!stepId) return;
  group.stepIds.add(stepId);
  const scope = stepScopeValue(stepId);
  if (scope) group.stepScopes.add(scope);
};

const goalGroupsFromSteps = (part: AgentExecutionResultPart): ArtifactGroup[] => {
  const groups = new Map<string, ArtifactGroup>();
  for (const step of part.execution.steps || []) {
    const goalId = stepGoalId(step);
    if (!goalId) continue;
    const group = groups.get(goalId) || emptyGroup("goal:" + goalId, goalId);
    addStepToGroup(group, step);
    groups.set(goalId, group);
  }
  return Array.from(groups.values());
};

const groupFromArtifact = (
  artifact: AgentResultArtifact,
  index: number
): ArtifactGroup => {
  const goalId = artifactGoalId(artifact);
  const group = emptyGroup(
    goalId
      ? "goal:" + goalId
      : "artifact:" + (artifactResourceId(artifact) || artifact.artifact_id || index),
    goalId || undefined
  );
  const stepId = artifactStepId(artifact);
  if (stepId) {
    group.stepIds.add(stepId);
    group.stepScopes.add(stepScopeValue(stepId));
  }
  group.artifacts.push(artifact);
  return group;
};

/**
 * Goal identity comes from the execution plan's projected step metadata.
 * Artifact-only grouping is retained only for older responses that predate
 * goal_id propagation; it still uses explicit relations, never array order.
 */
const projectArtifactGroups = (part: AgentExecutionResultPart): ArtifactGroup[] => {
  const business = (part.artifacts || []).filter(businessArtifact);
  const goalGroups = goalGroupsFromSteps(part);
  const groups: ArtifactGroup[] = [...goalGroups];
  const drafts = business.filter(artifact =>
    ["DRAFT", "POST_DRAFT", "CONTENT_DRAFT"].includes(artifactKind(artifact))
  );
  const failedScheduleScopes = (part.execution.steps || [])
    .filter(step =>
      ["FAILED", "FAILED_RETRYABLE"].includes(upper(step.status))
      && /SCHEDULE|MANAGE|PUBLISH/.test(stepKey(step))
    )
    .map(step => {
      const value = text(step.step_id);
      return value.includes(":") ? value.split(":")[0] : value;
    })
    .filter(Boolean);
  if (!groups.length) {
    if (!drafts.length && business.length < 2) return [];
    if (drafts.length) {
      groups.push(...drafts.map((draft, index) => {
        const group = groupFromArtifact(draft, index);
        group.id = "draft:" + (artifactResourceId(draft) || draft.artifact_id || index);
        return group;
      }));
    } else {
      const seeds = business.filter(artifact =>
        artifactGoalId(artifact)
        || artifactRelationValue(artifact, "task_id", "taskId")
        || stepScope(artifact)
      );
      const uniqueSeeds = new Map<string, AgentResultArtifact>();
      seeds.forEach((artifact, index) => {
        const key = artifactGoalId(artifact)
          || artifactRelationValue(artifact, "task_id", "taskId")
          || stepScope(artifact)
          || String(index);
        if (!uniqueSeeds.has(key)) uniqueSeeds.set(key, artifact);
      });
      groups.push(...Array.from(uniqueSeeds.values()).map(groupFromArtifact));
    }
  }

  for (const artifact of business) {
    if (groups.some(group => group.artifacts.includes(artifact))) continue;
    const group = attachableGroup(groups, artifact)
      || (groups.length === 1 ? groups[0] : undefined);
    if (group) {
      group.artifacts.push(artifact);
      const stepId = artifactStepId(artifact);
      if (stepId) {
        group.stepIds.add(stepId);
        group.stepScopes.add(stepScopeValue(stepId));
      }
    } else if (!goalGroups.length && groups.length > 1) {
      groups.push(groupFromArtifact(artifact, groups.length));
    }
  }

  if (!goalGroups.length) {
    for (const scope of failedScheduleScopes) {
      const alreadyRepresented = groups.some(group =>
        groupRelationKeys(group).includes("step:" + scope)
      );
      if (!alreadyRepresented && groups.some(group => groupRelationKeys(group).some(key => key.startsWith("step:")))) {
        const group = emptyGroup("step:" + scope);
        group.stepScopes.add(scope);
        groups.push(group);
      }
    }
  }
  return groups.length > 1 ? groups : [];
};

const resultExecutionStatus = (
  part: AgentExecutionResultPart,
  artifacts: AgentResultArtifact[],
  steps: ProjectionStep[] = part.execution.steps || []
): string => {
  const hasFailedArtifact = artifacts.some(artifact =>
    ["FAILED", "FAILED_RETRYABLE"].includes(upper(artifact.status))
  );
  const hasSuccessfulArtifact = artifacts.some(artifact =>
    !["FAILED", "FAILED_RETRYABLE"].includes(upper(artifact.status))
  );
  // ``steps`` is already scoped to this logical Goal when called from the
  // multi-goal projector. For a direct result it is the complete execution.
  const failedStepBelongsToGroup = steps.some(step =>
    ["FAILED", "FAILED_RETRYABLE"].includes(upper(step.status))
  );
  if (hasFailedArtifact || failedStepBelongsToGroup) return "FAILED";
  const executionStatus = upper(part.execution.status);
  const groupHasOpenStep = steps.some(step =>
    ["PENDING", "RUNNING", "WAITING_APPROVAL", "WAITING_HUMAN", "WAITING_DEPENDENCY"].includes(upper(step.status))
  );
  if (
    (hasSuccessfulArtifact || steps.some(step => upper(step.status) === "COMPLETED"))
    && ["FAILED", "WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(executionStatus)
    && !groupHasOpenStep
  ) return "COMPLETED";
  return part.execution.status;
};

const projectPartForArtifacts = (
  part: AgentExecutionResultPart,
  artifacts: AgentResultArtifact[],
  scopedSteps?: ProjectionStep[]
): AgentExecutionResultPart => {
  const artifactStepIds = new Set(artifacts.map(artifactStepId).filter(Boolean));
  const artifactScopes = new Set(artifacts.map(stepScope).filter(Boolean));
  const steps = scopedSteps || (artifactStepIds.size
    ? (part.execution.steps || []).filter(step => {
      const stepId = text(step.step_id);
      const scope = stepId.includes(":") ? stepId.split(":")[0] : stepId;
      return artifactStepIds.has(stepId) || artifactScopes.has(scope);
    })
    : part.execution.steps);
  return {
    ...part,
    artifacts,
    schedule: undefined,
    execution: {
      ...part.execution,
      status: resultExecutionStatus(part, artifacts, steps),
      steps
    }
  };
};

export const toUserFacingResult = (
  sourcePart: AgentExecutionResultPart,
  options: ResultProjectionOptions = {}
): UserFacingResult => {
  let part = sourcePart;
  if (options.artifacts || options.schedule !== undefined || options.steps || options.executionStatus) {
    part = {
      ...sourcePart,
      artifacts: options.artifacts ?? sourcePart.artifacts,
      schedule: options.schedule,
      execution: {
        ...sourcePart.execution,
        steps: options.steps ?? sourcePart.execution.steps,
        status: options.executionStatus ?? sourcePart.execution.status
      }
    };
  }

  const artifacts = part.artifacts || [];
  const draftArtifact = findArtifact(artifacts, ["DRAFT", "POST_DRAFT", "CONTENT_DRAFT"]);
  const scheduleArtifact = findArtifact(artifacts, ["SCHEDULE", "PUBLICATION_SCHEDULE"]);
  const postArtifact = findArtifact(artifacts, ["POST", "PUBLISHED_POST", "PUBLICATION"]);
  const searchArtifact = findArtifact(artifacts, ["SEARCH_RESULT"]);
  const analyticsArtifact = findArtifact(artifacts, ["ANALYSIS_REPORT", "PERFORMANCE_DATA"]);
  const schedulePayload = scheduleArtifact ? artifactPayload(scheduleArtifact) : {};
  const schedule = part.schedule || {};
  const draftId = draftArtifact
    ? artifactResourceId(draftArtifact)
    : text(scheduleArtifact?.draft_id || schedulePayload.draft_id || schedule.draft_id);
  const scheduleId = scheduleArtifact
    ? artifactResourceId(scheduleArtifact) || text(schedule.schedule_id)
    : text(schedule.schedule_id);
  const postId = postArtifact ? artifactResourceId(postArtifact) : "";
  const draftTitle = userFacingText(
    draftArtifact?.title || artifactPayload(draftArtifact).title,
    "未命名草稿"
  );
  const draftPreviewText = compact(
    userFacingText(
      draftArtifact?.summary
        || draftArtifact?.content
        || artifactPayload(draftArtifact).summary
        || artifactPayload(draftArtifact).content
    ),
    220
  );
  const draftPreview = draftPreviewText && draftPreviewText !== draftTitle
    ? draftPreviewText
    : undefined;
  const scheduledAt = userFacingText(
    scheduleArtifact?.run_at
      || scheduleArtifact?.publish_time
      || schedule.run_at
      || schedule.publish_time
      || schedulePayload.run_at
  ) || undefined;
  const timezone = getDisplayTimezone(
    text(scheduleArtifact?.timezone || schedule.timezone || schedulePayload.timezone)
  );
  const businessProjection = part.execution.business_projection;
  const businessState = businessProjection?.visible === false
    ? undefined
    : businessProjection?.state || undefined;
  const canonicalStatus = userFacingStatusFromBusinessProjection(businessProjection);
  const resultStatus = upper(scheduleArtifact?.status || schedule.status || part.execution.status);
  const failed = canonicalStatus === "FAILED"
    || (!canonicalStatus && upper(part.execution.status) === "FAILED");
  const failedKey = failedStepKey(part);
  const hasScheduleProjection = Boolean(scheduleArtifact || scheduleId || scheduledAt);
  const legacyScheduleFailed = /SCHEDULE|MANAGE/.test(failedKey)
    || (hasScheduleProjection && resultStatus === "FAILED");
  const scheduleFailed = canonicalStatus
    ? canonicalStatus === "FAILED" || canonicalStatus === "PARTIAL_SUCCESS"
    : legacyScheduleFailed;
  const hasDraftOutput = Boolean(draftArtifact || draftId);
  const partialSuccess = canonicalStatus === "PARTIAL_SUCCESS"
    || (!canonicalStatus && scheduleFailed && hasDraftOutput);
  const cancelled = businessState === "CANCELLED" || upper(part.execution.status) === "CANCELLED";
  const needsAction = ["WAITING_APPROVAL", "WAITING_HUMAN"].includes(upper(part.execution.status));
  // The Task-level completion comes from the backend's Goal satisfaction
  // projection. A single Execution completing (e.g. a Draft created) must not
  // mark the whole request "已完成" while its Scheduled Goal still misses a
  // Schedule. Older messages without task_status keep the legacy mapping.
  const taskStatus = upper(part.execution.task_status || "");
  const completionStatus: UserFacingStatus = canonicalStatus || (
    needsAction
      ? "NEEDS_ACTION"
      : taskStatus === "COMPLETED"
        ? "SUCCESS"
        : taskStatus === "FAILED" || taskStatus === "CANCELLED"
          ? "FAILED"
          : taskStatus
            ? "IN_PROGRESS"
            : "SUCCESS"
  );
  const controlKey = controlStepKey(part);
  const activity = stepActivity(part.execution.steps || []);
  const technical = makeTechnical(part);
  const baseSummary = draftPreview;

  if (controlKey) {
    const controlType = controlTypeFor(controlKey);
    const controlTitle: Record<ControlConfirmation["changeType"], string> = {
      PAUSE: "任务已暂停",
      RESUME: "任务已恢复",
      CANCEL: "发布计划已取消",
      SCHEDULE: "发布时间已更新",
      STATUS: "状态已更新",
      OTHER: "状态已更新"
    };
    const controlSummaryText: Record<ControlConfirmation["changeType"], string> = {
      PAUSE: "后续步骤暂时不会继续执行。",
      RESUME: "任务会继续处理，现有内容仍然保留。",
      CANCEL: "草稿仍然保留，之后可以再次安排发布。",
      SCHEDULE: "新的发布时间已经保存。",
      STATUS: "最新业务状态已经保存。",
      OTHER: "最新业务状态已经保存。"
    };
    return {
      type: "GENERIC_RESULT",
      status: completionStatus,
      title: controlTitle[controlType],
      summary: controlSummaryText[controlType],
      resourceId: draftId || scheduleId || postId || undefined,
      draft: draftId ? { draftId, title: draftTitle, preview: draftPreview } : undefined,
      schedule: hasScheduleProjection ? {
        scheduleId: scheduleId || "",
        draftId: draftId || undefined,
        scheduledAt,
        timezone,
        status: resultStatus
      } : undefined,
      actions: draftId ? [action(
        "view-draft",
        "查看草稿",
        `/create/manual?draftId=${encodeURIComponent(draftId)}`
      )] : [],
      hint: "可以继续直接告诉我下一步怎么处理。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (partialSuccess) {
    return {
      type: "DRAFT_CREATED",
      status: "PARTIAL_SUCCESS",
      title: "帖子已经准备好了，但发布安排没有成功",
      summary: "草稿已经保存，你的内容不会丢失。",
      resourceId: draftId || scheduleId || postId || undefined,
      draft: draftId ? { draftId, title: draftTitle, preview: draftPreview } : undefined,
      actions: failureActions(draftId),
      hint: "可以继续直接告诉我下一步怎么调整。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (failed) {
    return {
      type: "TASK_FAILED",
      status: "FAILED",
      title: "这次没有完成",
      summary: "主要结果没有生成，原有内容不会被本次失败覆盖。",
      resourceId: draftId || scheduleId || postId || undefined,
      draft: draftId ? { draftId, title: draftTitle, preview: draftPreview } : undefined,
      actions: failureActions(draftId),
      hint: "可以继续直接告诉我下一步怎么处理。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (scheduleFailed) {
    return {
      type: "TASK_FAILED",
      status: "FAILED",
      title: "发布安排没有成功",
      summary: "没有找到可继续使用的草稿，发布时间尚未安排。",
      resourceId: scheduleId || undefined,
      actions: failureActions(draftId),
      hint: "可以继续直接告诉我下一步怎么处理。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (postId) {
    return {
      type: "PUBLISHED_POST",
      status: completionStatus,
      title: "已发布",
      summary: baseSummary,
      resourceId: postId,
      post: { postId, title: draftTitle !== "未命名草稿" ? draftTitle : undefined },
      actions: [
        action("view-post", "查看帖子", `/post/${encodeURIComponent(postId)}`, "primary")
      ],
      hint: "如果你想继续分析这篇内容，直接告诉我即可。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (searchArtifact) {
    const items = toSearchItems(searchArtifact);
    const payload = artifactPayload(searchArtifact);
    const rawCount = payload.total ?? payload.count;
    const count = typeof rawCount === "number" ? rawCount : items.length || undefined;
    return {
      type: "SEARCH_RESULTS",
      status: completionStatus,
      title: count ? `找到 ${count} 篇相关内容` : "找到相关内容",
      summary: compact(userFacingText(searchArtifact.summary), 180) || undefined,
      search: { count, items },
      actions: [],
      hint: "想继续筛选或查询其他内容，直接告诉我即可。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (analyticsArtifact) {
    const analytics = toAnalytics(analyticsArtifact);
    return {
      type: "ANALYTICS_RESULT",
      status: completionStatus,
      title: "内容表现已整理",
      summary: compact(userFacingText(analyticsArtifact.summary), 180) || undefined,
      analytics,
      actions: [],
      hint: "想深入看某个指标或时间段，直接告诉我即可。",
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  if (draftArtifact || scheduleArtifact || draftId || scheduleId) {
    const scheduled = Boolean(scheduleId || scheduledAt);
    const type: UserFacingResultType = scheduled
      ? "SCHEDULED_POST"
      : hasRevisionStep(part)
        ? "CONTENT_REVISED"
        : "DRAFT_CREATED";
    const title = scheduled
      ? "已安排发布"
      : hasRevisionStep(part)
        ? "内容已更新"
        : "草稿已完成";
    const actions: UserFacingAction[] = [];
    if (draftId) {
      actions.push(action(
        "view-draft",
        "查看草稿",
        `/create/manual?draftId=${encodeURIComponent(draftId)}`,
        "primary"
      ));
    }
    return {
      type,
      status: completionStatus,
      title,
      summary: scheduled
        ? baseSummary || "帖子已经生成并保存为草稿，到时间后会自动发布。"
        : baseSummary,
      hint: scheduled
        ? "如果你想调整发布时间或取消发布，直接告诉我即可。"
        : "可以继续直接告诉我怎么调整。",
      resourceId: draftId || scheduleId || undefined,
      draft: draftId ? { draftId, title: draftTitle, preview: draftPreview } : undefined,
      schedule: scheduled
        ? {
            scheduleId: scheduleId || "",
            draftId: draftId || undefined,
            scheduledAt,
            timezone,
            status: resultStatus
          }
        : undefined,
      actions,
      activity,
      businessState,
      businessProjection: businessProjection || undefined,
      technical
    };
  }

  return {
    type: "GENERIC_RESULT",
    status: cancelled ? "SUCCESS" : completionStatus,
    title: cancelled ? "已停止" : "任务已完成",
    summary: cancelled ? "后续步骤不会继续执行。" : "结果已经整理完成。",
    actions: [],
    activity,
    businessState,
    businessProjection: businessProjection || undefined,
    technical
  };
};

const emptyTechnical = (): UserFacingResult["technical"] => ({
  executionId: "",
  status: "",
  steps: []
});

const wireRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" ? value as Record<string, unknown> : {};

const wireArray = (value: unknown): unknown[] => Array.isArray(value) ? value : [];

const wireValue = (record: Record<string, unknown>, ...keys: string[]): unknown => {
  for (const key of keys) {
    if (record[key] !== undefined && record[key] !== null) return record[key];
  }
  return undefined;
};

const safeHref = (value: unknown): string | undefined => {
  const href = text(value);
  return href.startsWith("/") || /^https?:\/\//i.test(href) ? href : undefined;
};

const structuredQueryInteraction = (
  part: AgentUserFacingInteractionPart
): Extract<UserFacingInteraction, { kind: "QUERY_RESULT" }> => {
  const wire = wireRecord(part.interaction.result);
  const search = wireRecord(wireValue(wire, "search"));
  const countValue = wireValue(search, "count");
  const count = typeof countValue === "number" ? countValue : undefined;
  const items = wireArray(wireValue(search, "items")).flatMap((value, index) => {
    const item = wireRecord(value);
    const title = userFacingText(
      wireValue(item, "title", "name"),
      "未命名内容"
    );
    if (!title) return [];
    return [{
      id: text(wireValue(item, "id", "resource_id", "resourceId")) || `result-${index}`,
      title,
      summary: compact(userFacingText(wireValue(item, "summary", "description")), 120) || undefined,
      href: safeHref(wireValue(item, "href", "url"))
    }];
  });
  const status = upper(wireValue(part.interaction, "status") || wireValue(wire, "status"));
  const result: UserFacingResult = {
    type: "SEARCH_RESULTS",
    status: status === "FAILED" ? "FAILED" : status === "PARTIAL_SUCCESS" ? "PARTIAL_SUCCESS" : "SUCCESS",
    language: text(wireValue(wire, "language")) || undefined,
    title: userFacingText(
      wireValue(wire, "title"),
      count ? `找到 ${count} 篇相关内容` : items.length ? "找到相关内容" : "没有找到足够相关的内容"
    ),
    summary: compact(userFacingText(wireValue(wire, "summary")), 180) || undefined,
    search: { count, items },
    actions: [],
    hint: text(wireValue(wire, "language")).toLowerCase().startsWith("en")
      ? "You can keep refining the search in natural language."
      : "想继续筛选或查询其他内容，直接告诉我即可。",
    activity: [],
    technical: emptyTechnical()
  };
  return { kind: "QUERY_RESULT", result };
};

const structuredSynthesisInteraction = (
  part: AgentUserFacingInteractionPart
): SynthesisInteraction => {
  const wire = wireRecord(part.interaction.synthesis);
  const sources = wireArray(wireValue(wire, "sources")).flatMap(value => {
    const source = wireRecord(value);
    const rawTitle = text(wireValue(source, "title"));
    if (containsInternalSourceReference(rawTitle)) return [];
    const title = userFacingProse(rawTitle, "未命名内容");
    if (!title) return [];
    const readStatus = upper(wireValue(source, "read_status", "readStatus"));
    const normalizedReadStatus: SynthesisSourceItem["readStatus"] =
      readStatus === "PARTIAL" || readStatus === "METADATA_ONLY" ? readStatus : "FULL";
    return [{
      resourceId: text(wireValue(source, "resource_id", "resourceId")) || undefined,
      title,
      excerpt: cleanSourceExcerpt(
        wireValue(source, "excerpt", "summary", "description"),
      ) || undefined,
      summary: cleanSourceExcerpt(
        wireValue(source, "excerpt", "summary", "description"),
      ) || undefined,
      href: safeHref(wireValue(source, "href", "url")),
      readStatus: normalizedReadStatus,
      sourceRefs: wireArray(wireValue(source, "source_refs", "sourceRefs"))
        .map(item => text(item))
        .filter(Boolean)
    }];
  });
  const knownRefs = new Set(
    sources
      .filter(source => source.readStatus !== "METADATA_ONLY")
      .flatMap(source => source.sourceRefs || [])
  );
  const pointList = (value: unknown, requireMultiple: boolean): SynthesisPoint[] =>
    wireArray(value).flatMap(item => {
      const point = wireRecord(item);
      const rawTitle = text(wireValue(point, "title"));
      const rawExplanation = text(wireValue(point, "explanation", "summary"));
      if (containsInternalSourceReference(`${rawTitle} ${rawExplanation}`)) return [];
      if (UNVERIFIED_HEDGE.test(`${rawTitle} ${rawExplanation}`)) return [];
      const refs = wireArray(wireValue(point, "source_refs", "sourceRefs"))
        .map(itemRef => text(itemRef))
        .filter(itemRef => knownRefs.has(itemRef))
        .filter(Boolean);
      if ((requireMultiple && refs.length < 2) || !refs.length) return [];
      const title = userFacingProse(rawTitle);
      const explanation = userFacingProse(rawExplanation);
      return title && explanation ? [{ title, explanation, sourceRefs: refs }] : [];
    });
  const status = upper(wireValue(part.interaction, "status") || wireValue(wire, "status"));
  return {
    kind: "SYNTHESIS_RESULT",
    status: status === "PARTIAL_SUCCESS" ? "PARTIAL_SUCCESS" : status === "FAILED" ? "FAILED" : "SUCCESS",
    synthesis: {
      title: userFacingText(wireValue(wire, "title"), "社区内容总结"),
      language: text(wireValue(wire, "language")) || undefined,
      intro: userFacingProse(wireValue(wire, "intro")) || undefined,
      totalMatched: typeof wireValue(wire, "total_matched", "totalMatched") === "number"
        ? wireValue(wire, "total_matched", "totalMatched") as number
        : undefined,
      selectedCount: typeof wireValue(wire, "selected_count", "selectedCount") === "number"
        ? wireValue(wire, "selected_count", "selectedCount") as number
        : undefined,
      readCount: typeof wireValue(wire, "read_count", "readCount") === "number"
        ? wireValue(wire, "read_count", "readCount") as number
        : undefined,
      failedCount: typeof wireValue(wire, "failed_count", "failedCount") === "number"
        ? wireValue(wire, "failed_count", "failedCount") as number
        : undefined,
      sources,
      commonPatterns: pointList(wireValue(wire, "common_patterns", "commonPatterns"), true),
      differences: pointList(wireValue(wire, "differences"), true),
      conclusion: UNVERIFIED_HEDGE.test(text(wireValue(wire, "conclusion")))
        ? undefined
        : userFacingProse(wireValue(wire, "conclusion")) || undefined,
      evidenceNote: userFacingProse(wireValue(wire, "evidence_note", "evidenceNote")) || undefined,
      navigation: wireArray(wireValue(wire, "navigation")).flatMap(value => {
        const link = wireRecord(value);
        const href = safeHref(wireValue(link, "href", "url"));
        const label = userFacingText(wireValue(link, "label"));
        return href && label ? [{ id: text(wireValue(link, "id")) || href, label, href }] : [];
      })
    },
    technical: emptyTechnical()
  };
};

export const projectUserFacingInteractionPart = (
  part: AgentUserFacingInteractionPart
): UserFacingInteraction => {
  if (part.interaction.kind === "SYNTHESIS_RESULT") return structuredSynthesisInteraction(part);
  return structuredQueryInteraction(part);
};

export const projectAgentMessageToUserFacingResult = (
  parts: AgentExecutionResultPart[]
): UserFacingResult[] => parts
  .filter(part => part.execution.business_projection?.visible !== false)
  .map(part => toUserFacingResult(part));

const navigationFromResult = (result: UserFacingResult): UserFacingLink[] =>
  result.actions.flatMap(item => item.kind === "link" && item.href ? [{
    id: item.id,
    label: item.label,
    href: item.href
  }] : []);

const artifactPayloadValues = (
  part: AgentExecutionResultPart,
  keys: string[]
): string[] => {
  const wanted = new Set(keys);
  return (part.artifacts || []).flatMap(artifact => {
    const payload = artifactPayload(artifact);
    return Object.entries(payload)
      .filter(([key]) => wanted.has(key))
      .map(([, value]) => userFacingText(value))
      .filter(Boolean);
  });
};

const formatScheduleFact = formatBusinessDateTime;

export const formatUserFacingScheduleTime = formatScheduleFact;

const changeTypeFor = (key: string): ChangeConfirmation["changeType"] => {
  if (/TITLE/.test(key)) return "TITLE";
  if (/SCHEDULE/.test(key)) return "SCHEDULE";
  if (/STATUS/.test(key)) return "STATUS";
  if (/CONTENT|IMPROVE|REVISE|MODIF|EDIT/.test(key)) return "CONTENT";
  return "OTHER";
};

const controlTypeFor = (key: string): ControlConfirmation["changeType"] => {
  if (/PAUSE|INTERRUPT/.test(key)) return "PAUSE";
  if (/RESUME/.test(key)) return "RESUME";
  if (/CANCEL/.test(key)) return "CANCEL";
  if (/SCHEDULE/.test(key)) return "SCHEDULE";
  if (/STATUS/.test(key)) return "STATUS";
  return "OTHER";
};

const changeSummary = (changeType: ChangeConfirmation["changeType"], result: UserFacingResult): string => ({
  CONTENT: "内容已更新",
  TITLE: "标题已更新",
  SCHEDULE: "发布时间已更新",
  STATUS: "状态已更新",
  OTHER: result.title
}[changeType]);

const controlSummary = (changeType: ControlConfirmation["changeType"], result: UserFacingResult): string => ({
  PAUSE: "任务已暂停",
  RESUME: "任务已恢复",
  CANCEL: "发布计划已取消",
  SCHEDULE: "发布时间已更新",
  STATUS: result.title,
  OTHER: result.title
}[changeType]);

const toChangeConfirmation = (
  part: AgentExecutionResultPart,
  result: UserFacingResult,
  key: string
): ChangeConfirmation => {
  const changeType = changeTypeFor(key);
  const previousValue = artifactPayloadValues(part, [
    "previous_title",
    "previous_content",
    "previous_value",
    "old_title",
    "old_content",
    "old_value"
  ])[0];
  const currentValue = changeType === "SCHEDULE"
    ? formatScheduleFact(result.schedule?.scheduledAt, result.schedule?.timezone)
    : result.draft?.preview;
  const unchangedFacts = artifactPayloadValues(part, ["unchanged_fact", "unchanged_facts"]);
  return {
    targetTitle: result.draft?.title || result.post?.title,
    changeType,
    summary: changeSummary(changeType, result),
    previousValue,
    currentValue,
    unchangedFacts: unchangedFacts.length ? unchangedFacts : undefined,
    navigation: navigationFromResult(result),
    hint: "可以继续直接告诉我下一步怎么调整。"
  };
};

const toControlConfirmation = (
  part: AgentExecutionResultPart,
  result: UserFacingResult,
  key: string
): ControlConfirmation => {
  const changeType = controlTypeFor(key);
  const preservedFacts = changeType === "CANCEL" && result.draft
    ? ["草稿内容仍然保留"]
    : undefined;
  return {
    targetTitle: result.draft?.title || result.post?.title,
    changeType,
    summary: controlSummary(changeType, result),
    currentStatus: result.schedule?.status
      ? statusLabel(result.schedule.status)
      : userFacingStatusText(result.status),
    preservedFacts,
    navigation: navigationFromResult(result),
    hint: "如果还要继续调整，直接告诉我即可。"
  };
};

export const projectUserFacingInteraction = (
  part: AgentExecutionResultPart
): UserFacingInteraction => {
  const result = toUserFacingResult(part);
  if (result.status === "FAILED") return { kind: "FAILURE_RESULT", result };

  const controlKey = controlStepKey(part);
  if (controlKey) {
    return {
      kind: "CONTROL_CONFIRMATION",
      result,
      control: toControlConfirmation(part, result, controlKey)
    };
  }

  const changeKey = changeStepKey(part);
  if (changeKey) {
    return {
      kind: "CHANGE_CONFIRMATION",
      result,
      change: toChangeConfirmation(part, result, changeKey)
    };
  }

  if (result.type === "SEARCH_RESULTS") return { kind: "QUERY_RESULT", result };
  if (result.type === "ANALYTICS_RESULT") return { kind: "ANALYSIS_RESULT", result };
  return { kind: "CONTENT_RESULT", result };
};

const resultFromInteraction = (
  interaction: UserFacingInteraction
): UserFacingResult | undefined => {
  switch (interaction.kind) {
    case "CONTENT_RESULT":
    case "QUERY_RESULT":
    case "ANALYSIS_RESULT":
    case "CHANGE_CONFIRMATION":
    case "CONTROL_CONFIRMATION":
    case "FAILURE_RESULT":
      return interaction.result;
    default:
      return undefined;
  }
};

const stepsForGroup = (
  part: AgentExecutionResultPart,
  group: ArtifactGroup
): ProjectionStep[] | undefined => {
  if (!group.stepIds.size && !group.stepScopes.size) return undefined;
  return (part.execution.steps || []).filter(step => {
    const stepId = text(step.step_id);
    return group.stepIds.has(stepId) || group.stepScopes.has(stepScopeValue(stepId));
  });
};

const aggregateResultStatus = (items: UserFacingGoalResult[]): UserFacingStatus => {
  const statuses = items.map(item => item.result.status);
  if (statuses.some(status => status === "NEEDS_ACTION")) return "NEEDS_ACTION";
  if (statuses.some(status => status === "PARTIAL_SUCCESS" || status === "FAILED")) {
    return statuses.every(status => status === "FAILED") ? "FAILED" : "PARTIAL_SUCCESS";
  }
  if (statuses.some(status => status === "IN_PROGRESS")) {
    return statuses.some(status => status === "SUCCESS") ? "PARTIAL_SUCCESS" : "IN_PROGRESS";
  }
  return "SUCCESS";
};

const resultGroupSummary = (
  items: UserFacingGoalResult[],
  status: UserFacingStatus
): string => {
  const count = items.length;
  if (status === "SUCCESS") return count + " 项内容都已经准备好了。";
  if (status === "NEEDS_ACTION") return count + " 项内容中有操作需要你的确认。";
  if (status === "PARTIAL_SUCCESS") return count + " 项内容已经生成，其中部分后续操作没有完成。";
  if (status === "IN_PROGRESS") return count + " 项内容正在处理中。";
  return "这次没有完成，已有内容仍然保留。";
};

const makeResultGroup = (
  items: UserFacingGoalResult[]
): ResultGroupInteraction => {
  const status = aggregateResultStatus(items);
  return {
    kind: "RESULT_GROUP",
    group: {
      title: status === "SUCCESS"
        ? items.length + " 项内容已准备好"
        : status === "PARTIAL_SUCCESS"
          ? items.length + " 项内容已生成"
          : status === "NEEDS_ACTION"
            ? "有内容需要你的确认"
            : "内容处理没有完成",
      summary: resultGroupSummary(items, status),
      status,
      items
    }
  };
};

export const projectAgentMessageToUserFacingInteractions = (
  parts: AgentExecutionResultPart[]
): UserFacingInteraction[] => {
  const projected = parts.flatMap(part => {
    if (part.execution.business_projection?.visible === false) return [];
    const groups = projectArtifactGroups(part);
    if (!groups.length) return [projectUserFacingInteraction(part)];
    const items = groups.flatMap(group => {
      const interaction = projectUserFacingInteraction(
        projectPartForArtifacts(part, group.artifacts, stepsForGroup(part, group))
      );
      const result = resultFromInteraction(interaction);
      return result ? [{ id: group.id, result }] : [];
    });
    return items.length > 1 ? [makeResultGroup(items)] : [projectUserFacingInteraction(part)];
  });

  const resultItems = projected.flatMap((interaction, index) => {
    if (interaction.kind === "RESULT_GROUP") {
      return interaction.group.items;
    }
    const result = resultFromInteraction(interaction);
    return result ? [{ id: result.resourceId || result.technical.executionId || String(index), result }] : [];
  });
  const nonResult = projected.filter(interaction =>
    interaction.kind !== "RESULT_GROUP" && !resultFromInteraction(interaction)
  );
  return resultItems.length > 1 && nonResult.length === 0
    ? [makeResultGroup(resultItems)]
    : projected;
};

export const projectAgentMessageToUserFacingInteraction = (
  part: AgentExecutionResultPart
): UserFacingInteraction => projectUserFacingInteraction(part);

export type ContentResult = Extract<UserFacingInteraction, { kind: "CONTENT_RESULT" }>;
export type QueryResult = Extract<UserFacingInteraction, { kind: "QUERY_RESULT" }>;
export type SynthesisResultInteraction = Extract<UserFacingInteraction, { kind: "SYNTHESIS_RESULT" }>;
export type AnalysisResult = Extract<UserFacingInteraction, { kind: "ANALYSIS_RESULT" }>;
export type ChangeInteraction = Extract<UserFacingInteraction, { kind: "CHANGE_CONFIRMATION" }>;
export type ControlInteraction = Extract<UserFacingInteraction, { kind: "CONTROL_CONFIRMATION" }>;
export type ApprovalRequest = Extract<UserFacingInteraction, { kind: "APPROVAL_REQUEST" }>;

const activityFromSteps = (
  steps: AgentRunStep[] | undefined,
  status: string
): AgentActivityItem[] => {
  const stepInputs = (steps || []).map(step => ({
    step_id: step.step_id,
    label: step.label,
    capability: [step.tool_name, step.kind, ...(step.capabilities || [])]
      .filter(Boolean)
      .join(" "),
    status: step.status,
    error: step.error
  }));
  const projected = stepActivity(stepInputs);
  if (projected.length) {
    const retrievalContext = stepInputs.some(step => /SEARCH|FIND|LIST_OWN_POSTS|COMMUNITY/.test(stepKey(step)))
      && stepInputs.some(step => /GET_POST|POST_DETAIL|READ_POST|READ_SOURCE|DETAIL/.test(stepKey(step)));
    const hasSynthesisStep = stepInputs.some(
      step => semanticStepKey(step, retrievalContext) === "SYNTHESIZE_RESULTS"
    );
    const hasOpenStep = projected.some(item => item.status === "active" || item.status === "pending");
    if (
      retrievalContext
      && !hasSynthesisStep
      && !hasOpenStep
      && ["RUNNING", "RETRYING"].includes(status)
    ) {
      return [
        ...projected,
        { id: "synthesis", label: STEP_COPY.SYNTHESIZE_RESULTS.active, status: "active" }
      ];
    }
    return projected;
  }
  if (status === "WAITING_APPROVAL" || status === "WAITING_HUMAN") {
    return [{ id: "approval", label: "等待你的确认", status: "active" }];
  }
  if (status === "PAUSED") return [{ id: "paused", label: "已暂停，可以继续", status: "pending" }];
  if (status === "FAILED") return [{ id: "failed", label: "这次没有完成", status: "failed" }];
  return [{ id: "working", label: "正在准备内容…", status: "active" }];
};

/**
 * LEGACY_FALLBACK: inferred Execution/Step activity is no longer rendered in
 * the ordinary Agent panel. Durable UserActivityEvent is the live source.
 */
export const projectExecutionActivity = (execution: Execution): AgentActivityItem[] => {
  const status = upper(execution.status);
  if (upper(execution.error_code) === "RESULT_UNKNOWN") {
    return [{ id: "verifying", label: "正在确认操作结果", status: "active" }];
  }
  if (["WAITING_EXTERNAL", "PROCESSING"].includes(status)) {
    return [{ id: "processing", label: "正在等待外部操作完成", status: "active" }];
  }
  const stepInputs = (execution.steps || []).map(step => ({
    step_id: step.step_id,
    capability: step.capability,
    status: step.status,
    error: step.error_message || null
  }));
  const projected = stepActivity(stepInputs);
  if (projected.length) return projected;
  if (status === "WAITING_APPROVAL" || status === "WAITING_HUMAN") {
    return [{ id: "approval", label: "等待你的确认", status: "active" }];
  }
  if (status === "PAUSED" || execution.control_state === "PAUSED") {
    return [{ id: "paused", label: "已暂停，可以继续", status: "pending" }];
  }
  if (status === "FAILED") return [{ id: "failed", label: "这次没有完成", status: "failed" }];
  if (status === "COMPLETED") return [{ id: "result", label: "正在整理结果…", status: "active" }];
  const current = text(execution.current_step);
  return [{
    id: current || "working",
    label: current ? `${stepLabel({ capability: current })}…` : "正在准备内容…",
    status: "active"
  }];
};

/** LEGACY_FALLBACK: see projectExecutionActivity above. */
export const projectRunActivity = (run: AgentRun): AgentActivityItem[] =>
  activityFromSteps(run.steps, upper(run.status));

export const approvalPresentation = (run: AgentRun) => {
  const approval = run.approval;
  const operation = upper(approval?.action);
  const isDelete = operation.includes("DELETE") || operation.includes("REMOVE");
  const isPublish = operation.includes("PUBLISH") || operation.includes("PUBLICATION");
  const scheduleArtifact = (run.artifacts || []).find(item => {
    const kind = upper(item.resource_type || item.type);
    return ["SCHEDULE", "PUBLICATION_SCHEDULE"].includes(kind);
  });
  const schedulePayload = artifactPayload(scheduleArtifact);
  const approvalPreview = approval?.preview || {};
  const plannedTime = formatScheduleFact(
    text(
      scheduleArtifact?.run_at
        || scheduleArtifact?.publish_time
        || schedulePayload.run_at
        || schedulePayload.publish_time
        || approvalPreview.run_at
        || approvalPreview.publish_time
    ),
    getDisplayTimezone(text(
      scheduleArtifact?.timezone
        || schedulePayload.timezone
        || approvalPreview.timezone
    ))
  );
  const draftArtifact = (run.artifacts || []).find(item => {
    const kind = upper(item.resource_type || item.type);
    return ["DRAFT", "POST_DRAFT", "CONTENT_DRAFT"].includes(kind);
  });
  const draftPreview = compact(userFacingText(
    draftArtifact?.summary
      || draftArtifact?.content
      || artifactPayload(draftArtifact).summary
      || artifactPayload(draftArtifact).content
  ), 220) || undefined;
  const resourceId = text(
    approvalPreview.draft_id
      || approvalPreview.post_id
      || approvalPreview.resource_id
      || approvalPreview.schedule_id
  );
  const artifact = (run.artifacts || []).find(item => {
    const kind = upper(item.resource_type || item.type);
    return resourceId && artifactResourceId(item) === resourceId
      || (!resourceId && ["DRAFT", "POST", "POST_DRAFT"].includes(kind));
  });
  const resourceTitle = userFacingText(
    artifact?.title || approvalPreview.target_title,
    "待确认草稿"
  );
  const scheduledPublish = Boolean(plannedTime) || operation.includes("SCHEDULE");
  return {
    title: isDelete ? "删除前需要你的确认" : "需要你的确认",
    actionTitle: isPublish
      ? scheduledPublish ? "准备按计划发布" : "准备立即发布"
      : isDelete ? "准备删除内容" : "准备执行这项操作",
    resourceTitle,
    draftPreview,
    plannedTime,
    resourceId: resourceId || artifactResourceId(artifact),
    description: userFacingText(approval?.description, "这项操作会改变你的内容状态。"),
    consequence: isPublish
      ? plannedTime
        ? `计划在 ${plannedTime} 发布；确认后，社区用户将可以看到这篇内容。`
        : "确认后，社区用户将可以看到这篇内容。"
      : isDelete
        ? "确认后，这项内容将从你的社区内容中移除。"
        : "确认后，Agent 会继续完成这项操作。",
    confirmLabel: isPublish ? "确认发布" : isDelete ? "确认删除" : "确认执行",
    isDelete,
    canConfirm: true,
    canReject: true,
    canModify: true
  };
};

export const projectAgentRunToUserFacingInteraction = (
  run: AgentRun
): Extract<UserFacingInteraction, { kind: "APPROVAL_REQUEST" }> | null => {
  const approvalStatus = upper(run.approval?.status);
  if (
    !run.approval
    || (approvalStatus && !["PENDING", "WAITING_APPROVAL", "WAITING_HUMAN"].includes(approvalStatus))
  ) return null;
  const presentation = approvalPresentation(run);
  return {
    kind: "APPROVAL_REQUEST",
    approval: {
      ...presentation,
      technical: {
        runId: run.run_id,
        approvalId: run.approval.approval_id,
        action: run.approval.action,
        resourceId: presentation.resourceId
      }
    }
  };
};

export const projectAgentRunArtifactsToUserFacingInteractions = (
  run: AgentRun
): UserFacingInteraction[] => {
  if (!run.artifacts?.length) return [];
  const part: AgentExecutionResultPart = {
    type: "execution_result",
    execution: {
      execution_id: text(run.execution_id || run.run_id),
      task_id: text(run.task_ledger.task_id),
      status: run.status,
      summary: text(run.goal || run.summary),
      steps: (run.steps || []).map(step => ({
        step_id: step.step_id,
        goal_id: step.goal_id,
        capability: step.capabilities?.[0],
        label: step.label,
        status: step.status,
        error: step.error
      }))
    },
    artifacts: run.artifacts,
    next_actions: []
  };
  return projectAgentMessageToUserFacingInteractions([part]);
};

const isWaitingApprovalStatus = (status?: string | null): boolean =>
  ["WAITING_APPROVAL", "WAITING_HUMAN"].includes(upper(status));

export const projectPendingApprovalFallback = (
  run?: AgentRun | null,
  execution?: Execution | null
): Extract<UserFacingInteraction, { kind: "APPROVAL_REQUEST" }> | null => {
  if (!isWaitingApprovalStatus(run?.status) && !isWaitingApprovalStatus(execution?.status)) {
    return null;
  }
  const draft = (run?.artifacts || []).find(item => {
    const kind = upper(item.resource_type || item.type);
    return ["DRAFT", "POST_DRAFT", "CONTENT_DRAFT"].includes(kind);
  });
  const draftPayload = artifactPayload(draft);
  const draftPreview = compact(userFacingText(
    draft?.summary
      || draft?.content
      || draftPayload.summary
      || draftPayload.content
  ), 220) || undefined;
  const resourceTitle = userFacingText(draft?.title, "这项操作");
  const technicalRunId = text(run?.run_id || execution?.run_id || execution?.execution_id);
  return {
    kind: "APPROVAL_REQUEST",
    approval: {
      title: "需要你的确认",
      actionTitle: "确认信息暂时无法加载",
      resourceTitle,
      draftPreview,
      description: "当前操作正在等待确认，但审批详情暂时无法加载。",
      consequence: "你可以继续输入修改内容；确认信息恢复后再决定是否继续。",
      confirmLabel: "确认",
      isDelete: false,
      canConfirm: false,
      canReject: false,
      canModify: false,
      technical: {
        runId: technicalRunId
      }
    }
  };
};

export const projectTargetClarification = (
  part: AgentTargetClarificationPart
): Extract<UserFacingInteraction, { kind: "ASK_USER" }> => ({
  kind: "ASK_USER",
  clarification: {
    question: "请选择要操作的目标",
    description: "我找到多个可能的目标。选定后会继续原来的请求，不会创建重复任务。",
    candidates: part.candidates.map(candidate => ({
      identity: candidate.identity,
      type: candidate.type,
      label: userFacingText(candidate.label, `未命名${targetTypeLabel(candidate.type)}`),
      status: userFacingStatusLabel(candidate.status || candidate.type)
    }))
  }
});
