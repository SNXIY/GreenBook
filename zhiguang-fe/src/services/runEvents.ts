/**
 * Canonical Run event vocabulary (mirror of
 * `packages/contracts/greenbook_contracts/events.py`).
 *
 * The backend pushes these event_type names over
 * `GET /api/v1/agent/runs/{run_id}/stream` (SSE). Keep this file in sync with
 * the Python constants; payload shapes are documented in
 * `docs/architecture/SERVICE_COMMUNICATION.md`.
 */
export const RUN_EVENT = {
  UNDERSTANDING: "UNDERSTANDING",
  SEMANTIC_ACTION: "SEMANTIC_ACTION_SELECTED",
  ACTION_COMPLETED: "ACTION_COMPLETED",
  PARTIAL_RESULT: "PARTIAL_RESULT",
  FOLLOW_UP_QUEUED: "FOLLOW_UP_QUEUED",
  REASONING_STARTED: "REASONING_STARTED",
  TOOL_STARTED: "TOOL_STARTED",
  OBSERVATION: "OBSERVATION_RECEIVED",
  WAITING_APPROVAL: "WAITING_APPROVAL",
  RUN_COMPLETED: "RUN_COMPLETED",
  RUN_FAILED: "RUN_FAILED"
} as const;

export type RunEventType = (typeof RUN_EVENT)[keyof typeof RUN_EVENT];
