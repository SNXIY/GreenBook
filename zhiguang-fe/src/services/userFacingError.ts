/**
 * Converts transport/runtime failures into copy suitable for the ordinary
 * product surface. The original error remains available to logs and the
 * caller's observability hooks; it must not be rendered as UI copy.
 */
type ErrorLike = {
  status?: unknown;
  message?: unknown;
  data?: unknown;
};

const INTERNAL_MARKERS = [
  "TARGET_RESOLUTION_AMBIGUOUS",
  "TEMPORAL_NOT_RESOLVED",
  "RESULT_UNKNOWN",
  "WAITING_EXTERNAL",
  "TOOL_ARGUMENT_VALIDATION_FAILED",
  "execution_id",
  "operation_id",
  "objective_id",
  "task_id",
  "run_id",
  "trace_id",
  "operationledger",
  "fencing token",
  "mcp",
  "stack trace",
  "queue retry",
  "worker"
];

const textFrom = (value: unknown): string => {
  if (typeof value === "string") return value.trim();
  if (!value || typeof value !== "object") return "";
  const record = value as Record<string, unknown>;
  return typeof record.message === "string" ? record.message.trim() : "";
};

const isInternal = (value: string): boolean => {
  const lower = value.toLowerCase();
  return INTERNAL_MARKERS.some(marker => lower.includes(marker.toLowerCase()))
    || /^\s*(?:\{|\[)/.test(value)
    || /\b(?:4\d{2}|5\d{2})\b/.test(value)
    || /failed to fetch|network error|aborterror/i.test(value);
};

export const userFacingErrorMessage = (
  cause: unknown,
  fallback = "操作暂时无法完成，请稍后重试。"
): string => {
  const error = cause as ErrorLike | null | undefined;
  const status = typeof error?.status === "number" ? error.status : undefined;
  if (status === 401 || status === 403) return "登录状态已失效，请重新登录后继续。";
  if (status === 404) return "内容不存在或已不可用。";
  if (status === 409) return "内容刚刚发生变化，请刷新后重试。";

  const raw = textFrom(error?.message) || textFrom(error?.data) || textFrom(cause);
  if (!raw || isInternal(raw) || raw.length > 160) return fallback;

  if (/invalid credentials|invalid username|password is incorrect|用户名或密码错误/i.test(raw)) {
    return "账号或密码不正确，请检查后重试。";
  }

  if (/timeout|timed out|超时/i.test(raw)) return "服务响应较慢，请稍后查看结果。";
  return raw.replace(/^请求失败[：: ]*/u, "").trim() || fallback;
};
