import { formatBusinessDateTime, getDisplayTimezone } from "@/utils/dateTime";
import type { UserActivityEvent } from "@/types/userActivity";

export type ActivityGroup = {
  key: string;
  title: string;
  events: UserActivityEvent[];
};

const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";

const number = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

export const activityLabel = (event: UserActivityEvent): string => {
  const payload = event.safe_payload || {};
  const title = text(payload.title);
  const description = text(payload.description);
  const question = text(payload.question);
  const count = number(payload.result_count);
  const scheduleTime = text(payload.run_at);
  const timezone = text(payload.timezone) || getDisplayTimezone();
  const formattedTime = scheduleTime
    ? formatBusinessDateTime(scheduleTime, timezone) || scheduleTime
    : "";

  switch (event.activity_type) {
    case "SEARCH_STARTED": return "正在搜索相关内容";
    case "SEARCH_COMPLETED": return count === null ? "已完成相关内容检索" : `已找到 ${count} 条相关内容`;
    case "SUMMARIZATION_STARTED": return "正在整理主要信息";
    case "SUMMARIZATION_COMPLETED": return "已整理主要信息";
    case "DRAFT_LOOKUP_STARTED": return "正在查找草稿";
    case "DRAFT_LOOKUP_COMPLETED": return "已找到草稿";
    case "DRAFT_CREATING": return "正在生成草稿";
    case "DRAFT_CREATED": return title ? `草稿已创建：${title}` : "草稿已创建";
    case "DRAFT_UPDATING": return title ? `正在修改《${title}》` : "正在修改草稿";
    case "DRAFT_UPDATED": return title ? `内容已更新：${title}` : "草稿内容已更新";
    case "DRAFT_DELETING": return title ? `正在删除《${title}》` : "正在删除草稿";
    case "DRAFT_DELETED": return title ? `已删除《${title}》` : "草稿已删除";
    case "SCHEDULE_LOOKUP_STARTED": return "正在查询发布时间";
    case "SCHEDULE_LOOKUP_COMPLETED": return "已查询发布时间";
    case "SCHEDULE_CREATING": return "正在安排发布";
    case "SCHEDULE_CREATED": return formattedTime ? `已安排 ${formattedTime} 发布` : "已安排发布";
    case "SCHEDULE_UPDATING": return "正在修改发布时间";
    case "SCHEDULE_UPDATED": return formattedTime ? `发布时间已改为 ${formattedTime}` : "发布时间已更新";
    case "SCHEDULE_CANCELLING": return "正在取消发布安排";
    case "SCHEDULE_CANCELLED": return "已取消发布安排，草稿会保留";
    case "PUBLISHING": return "正在发布";
    case "PUBLISHED": return "已发布";
    case "REPLYING": return "正在回复互动";
    case "REPLIED": return "已完成回复";
    case "ANALYTICS_LOADING": return "正在读取内容数据";
    case "ANALYTICS_COMPLETED": return "已更新内容数据";
    case "NEEDS_CLARIFICATION": return question || "需要你确认要操作的内容";
    case "NEEDS_APPROVAL": return description ? `请确认：${description}` : "需要你的确认后才能继续";
    case "RESULT_UNKNOWN": return "正在确认操作结果";
    case "RECONCILING": return "正在确认操作结果";
    case "FAILED": return text(payload.message) || "这项操作暂时没有完成";
    default: return "正在处理";
  }
};

export const groupActivities = (activities: UserActivityEvent[]): ActivityGroup[] => {
  const groups = new Map<string, ActivityGroup>();
  for (const event of [...activities].sort((left, right) => left.sequence - right.sequence)) {
    if (event.activity_type === "NEEDS_SEMANTIC_CONFIRMATION") continue;
    const key = event.task_id || event.run_id || "conversation";
    const existing = groups.get(key);
    const title = text(event.safe_payload.title) || existing?.title || "当前内容";
    if (existing) {
      existing.title = title;
      existing.events.push(event);
    } else {
      groups.set(key, { key, title, events: [event] });
    }
  }
  return [...groups.values()];
};

export const candidateLabels = (event: UserActivityEvent): string[] => {
  const candidates = event.safe_payload.candidates;
  if (!Array.isArray(candidates)) return [];
  return candidates
    .map(candidate => candidate && typeof candidate === "object"
      ? text((candidate as Record<string, unknown>).label)
      : "")
    .filter(Boolean)
    .slice(0, 5);
};
