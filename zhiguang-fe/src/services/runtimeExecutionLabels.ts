import type { ExecutionStatus, ExecutionStepStatus } from "@/types/execution";

const executionStatusLabels: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  RETRYING: "重试中",
  WAITING_DEPENDENCY: "等待依赖",
  PAUSED: "已暂停",
  WAITING_APPROVAL: "等待确认",
  WAITING_HUMAN: "等待人工处理",
  COMPLETED: "已完成",
  FAILED: "执行失败",
  CANCELLED: "已取消"
};

const stepStatusLabels: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  WAITING_DEPENDENCY: "等待依赖",
  WAITING_APPROVAL: "等待确认",
  COMPLETED: "已完成",
  FAILED_RETRYABLE: "失败，可重试",
  FAILED: "执行失败",
  SKIPPED: "已跳过"
};

const stepLabels: Record<string, string> = {
  SEARCH_COMMUNITY: "搜索参考资料",
  ANALYZE_CONTENT_PATTERNS: "分析内容",
  GENERATE_CONTENT: "生成内容",
  IMPROVE_CONTENT: "修改内容",
  VALIDATE_QUALITY: "校验质量",
  SCHEDULE_PUBLISH: "安排发布",
  MANAGE_SCHEDULE: "调整发布时间",
  CANCEL_SCHEDULE: "取消发布计划"
};

export const runtimeExecutionStatusLabel = (status: ExecutionStatus | string) =>
  executionStatusLabels[status] ?? "状态已更新";

export const runtimeStepStatusLabel = (status: ExecutionStepStatus | string) =>
  stepStatusLabels[status] ?? "状态已更新";

export const runtimeStepLabel = (step: string) => {
  if (stepLabels[step]) return stepLabels[step];
  if (!step) return "";
  return /^[A-Z0-9_-]+$/.test(step) ? "处理任务" : step;
};

export const runtimeExecutionTitle = (currentStep?: string | null, taskId?: string) =>
  (currentStep && runtimeStepLabel(currentStep)) || taskId || "Runtime 执行";

export const runtimeExecutionButtonLabels = {
  retry: "重试步骤",
  retrying: "正在重试…",
  pause: "暂停",
  pausing: "正在暂停…",
  resume: "继续执行",
  resuming: "正在恢复…",
  cancel: "取消"
} as const;

export const runtimeExecutionMetaLabels = {
  details: "执行详情",
  executionId: "执行 ID",
  taskId: "任务 ID",
  planId: "计划 ID",
  progress: "进度",
  events: "事件",
  failed: "Runtime 执行失败"
} as const;
