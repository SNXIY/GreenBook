import type { UserActivityEvent } from "./userActivity";

export type SemanticConfirmationAction = "CONFIRM" | "MODIFY" | "CANCEL";

export type SemanticConfirmationTarget = {
  kind?: string;
  label?: string;
};

export type SemanticConfirmationObjective = {
  topic: string;
  desired_outcome: string;
  outcome: string;
  target?: SemanticConfirmationTarget;
  run_at?: string | null;
  timezone?: string | null;
  publication_intent?: string | null;
  dependencies: string[];
  has_real_side_effect: boolean;
};

export type SemanticConfirmationPayload = {
  confirmation_id: string;
  task_version: number;
  confirmation_version: number;
  title: string;
  objectives: SemanticConfirmationObjective[];
  has_real_side_effect: boolean;
  available_actions: SemanticConfirmationAction[];
  policy_reason?: string;
};

export type SemanticConfirmationControl = {
  action: SemanticConfirmationAction;
  confirmation_id: string;
  expected_task_version: number;
  expected_confirmation_version: number;
  modification?: { text: string };
};

export type SemanticConfirmationControlResponse = {
  task_id: string;
  action: SemanticConfirmationAction;
  status: string;
  confirmation_state: string;
  task_version: number;
  confirmation_version: number;
  confirmed_version?: number | null;
  idempotent: boolean;
  resume_queued: boolean;
  requires_new_compilation: boolean;
};

export type SemanticConfirmationViewState =
  | "WAITING_CONFIRMATION"
  | "CONFIRMING"
  | "CANCELLING"
  | "CONFIRMED"
  | "WORKING"
  | "MODIFYING"
  | "CANCELLED"
  | "STALE";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === "object" && !Array.isArray(value);

const text = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";

const number = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

const boolean = (value: unknown): boolean => value === true;

const action = (value: unknown): SemanticConfirmationAction | null => {
  if (value === "CONFIRM" || value === "MODIFY" || value === "CANCEL") return value;
  return null;
};

const projectObjective = (value: unknown): SemanticConfirmationObjective | null => {
  if (!isRecord(value)) return null;
  const target = isRecord(value.target)
    ? {
        ...(text(value.target.kind) ? { kind: text(value.target.kind) } : {}),
        ...(text(value.target.label) ? { label: text(value.target.label) } : {})
      }
    : undefined;
  const dependencies = Array.isArray(value.dependencies)
    ? value.dependencies.filter((item): item is string => Boolean(text(item))).map(item => text(item))
    : [];
  return {
    topic: text(value.topic),
    desired_outcome: text(value.desired_outcome),
    outcome: text(value.outcome) || text(value.desired_outcome),
    ...(target && Object.keys(target).length ? { target } : {}),
    run_at: text(value.run_at) || null,
    timezone: text(value.timezone) || null,
    publication_intent: text(value.publication_intent) || null,
    dependencies,
    has_real_side_effect: boolean(value.has_real_side_effect)
  };
};

export const isSemanticConfirmationEvent = (event: UserActivityEvent): boolean =>
  event.activity_type === "NEEDS_SEMANTIC_CONFIRMATION"
  && event.status === "WAITING_SEMANTIC_CONFIRMATION";

export const projectSemanticConfirmation = (
  event: UserActivityEvent
): SemanticConfirmationPayload | null => {
  if (!isSemanticConfirmationEvent(event)) return null;
  const raw = event.safe_payload || {};
  const confirmationId = text(raw.confirmation_id);
  const taskVersion = number(raw.task_version);
  const confirmationVersion = number(raw.confirmation_version);
  if (!confirmationId || taskVersion === null || confirmationVersion === null) return null;
  const objectives = Array.isArray(raw.objectives)
    ? raw.objectives.map(projectObjective).filter((item): item is SemanticConfirmationObjective => item !== null)
    : [];
  const availableActions: SemanticConfirmationAction[] = Array.isArray(raw.available_actions)
    ? raw.available_actions.map(action).filter((item): item is SemanticConfirmationAction => item !== null)
    : ["CONFIRM", "MODIFY", "CANCEL"];
  return {
    confirmation_id: confirmationId,
    task_version: taskVersion,
    confirmation_version: confirmationVersion,
    title: text(raw.title) || "确认这项安排",
    objectives,
    has_real_side_effect: boolean(raw.has_real_side_effect),
    available_actions: availableActions,
    ...(text(raw.policy_reason) ? { policy_reason: text(raw.policy_reason) } : {})
  };
};

export const semanticConfirmationKey = (
  event: UserActivityEvent,
  payload: SemanticConfirmationPayload
): string => payload.confirmation_id || event.activity_id;

/** Keep one immutable snapshot per Task; the highest activity sequence wins. */
export const selectLatestSemanticConfirmationEvents = (
  events: UserActivityEvent[]
): UserActivityEvent[] => {
  const latest = new Map<string, UserActivityEvent>();
  for (const event of events) {
    const payload = projectSemanticConfirmation(event);
    if (!payload) continue;
    const taskKey = text(event.task_id) || payload.confirmation_id;
    const current = latest.get(taskKey);
    if (!current || event.sequence >= current.sequence) latest.set(taskKey, event);
  }
  return [...latest.values()].sort((left, right) => left.sequence - right.sequence);
};

export const buildSemanticConfirmationControl = (
  payload: SemanticConfirmationPayload,
  actionName: SemanticConfirmationAction,
  modification?: string
): SemanticConfirmationControl => ({
  action: actionName,
  confirmation_id: payload.confirmation_id,
  expected_task_version: payload.task_version,
  expected_confirmation_version: payload.confirmation_version,
  ...(actionName === "MODIFY" && modification?.trim()
    ? { modification: { text: modification.trim() } }
    : {})
});

export const semanticConfirmationErrorMessage = (status?: number): string =>
  status === 409
    ? "这项安排已经发生变化，请以最新版本为准。"
    : "确认请求暂时没有完成，请稍后重试。";
