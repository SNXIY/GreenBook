import assert from "node:assert/strict";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import SemanticConfirmationCard from "../src/components/agent/SemanticConfirmationCard";
import { AgentApiError, agentService } from "../src/services/agentService";
import {
  buildSemanticConfirmationControl,
  projectSemanticConfirmation,
  selectLatestSemanticConfirmationEvents,
  semanticConfirmationErrorMessage
} from "../src/types/semanticConfirmation";
import type { UserActivityEvent } from "../src/types/userActivity";

const event = (overrides: Partial<UserActivityEvent> = {}): UserActivityEvent => ({
  activity_id: "activity-confirmation-1",
  conversation_id: "conversation-1",
  task_id: "task-internal",
  activity_type: "NEEDS_SEMANTIC_CONFIRMATION",
  status: "WAITING_SEMANTIC_CONFIRMATION",
  display_key: "activity.semantic_confirmation.required",
  safe_payload: {
    confirmation_id: "confirmation-internal",
    task_version: 8,
    confirmation_version: 2,
    title: "两篇帖子",
    has_real_side_effect: true,
    available_actions: ["CONFIRM", "MODIFY", "CANCEL"],
    objectives: [
      {
        topic: "Java 后端实习面试最容易被问到的 10 个问题",
        desired_outcome: "Java 后端实习面试最容易被问到的 10 个问题",
        outcome: "立即发布",
        target: { kind: "POST", label: "Java 原帖", resource_id: "hidden-resource" },
        run_at: null,
        timezone: "Asia/Shanghai",
        publication_intent: "IMMEDIATE_PUBLISH",
        dependencies: [],
        has_real_side_effect: true
      },
      {
        topic: "2026 年 Agent 开发需要掌握哪些核心技术",
        desired_outcome: "2026 年 Agent 开发需要掌握哪些核心技术",
        outcome: "定时发布",
        target: { kind: "DRAFT", label: "Agent 草稿", resource_id: "hidden-resource-2" },
        run_at: "2026-08-22T06:05:00Z",
        timezone: "Asia/Shanghai",
        publication_intent: "SCHEDULED_PUBLISH",
        dependencies: ["先完成第一篇内容"],
        has_real_side_effect: true
      }
    ],
    execution_id: "hidden-execution",
    operation_id: "hidden-operation",
    capability: "PUBLISH_NOW"
  },
  sequence: 10,
  created_at: "2026-08-21T00:00:00Z",
  terminal: true,
  ...overrides
});

const projected = projectSemanticConfirmation(event());
assert.ok(projected);
assert.equal(projected?.objectives.length, 2);
assert.equal(projected?.objectives[0]?.topic, "Java 后端实习面试最容易被问到的 10 个问题");
assert.equal(projected?.objectives[1]?.outcome, "定时发布");
assert.equal(projected?.objectives[1]?.run_at, "2026-08-22T06:05:00Z");
assert.equal(projected?.objectives[1]?.timezone, "Asia/Shanghai");
assert.deepEqual(projected?.objectives[0]?.target, { kind: "POST", label: "Java 原帖" });
assert.equal(projected?.objectives[1]?.dependencies[0], "先完成第一篇内容");
// The typed control identity/version stay in in-memory request state; only
// the card projection must omit task/resource/runtime internals.
assert.doesNotMatch(JSON.stringify(projected?.objectives), /task-internal|confirmation-internal|hidden-resource|hidden-execution|hidden-operation|PUBLISH_NOW/);

const renderedCard = renderToStaticMarkup(React.createElement(SemanticConfirmationCard, {
  event: event(),
  onConfirm: () => undefined,
  onCancel: () => undefined,
  onModify: () => undefined
}));
assert.match(renderedCard, /Java/);
assert.match(renderedCard, /2026/);
assert.equal((renderedCard.match(/<button/g) || []).length, 3);
assert.doesNotMatch(renderedCard, /task-internal|confirmation-internal|hidden-resource|hidden-execution|hidden-operation|PUBLISH_NOW/);

const ordinaryEvent = event({
  activity_type: "SEARCH_COMPLETED",
  status: "COMPLETED",
  safe_payload: { result_count: 2 }
});
assert.equal(projectSemanticConfirmation(ordinaryEvent), null);
assert.equal(selectLatestSemanticConfirmationEvents([ordinaryEvent]).length, 0);

const control = buildSemanticConfirmationControl(projected!, "CONFIRM");
assert.deepEqual(control, {
  action: "CONFIRM",
  confirmation_id: "confirmation-internal",
  expected_task_version: 8,
  expected_confirmation_version: 2
});
assert.deepEqual(
  buildSemanticConfirmationControl(projected!, "MODIFY", "把第二篇改成明天早上发布"),
  {
    action: "MODIFY",
    confirmation_id: "confirmation-internal",
    expected_task_version: 8,
    expected_confirmation_version: 2,
    modification: { text: "把第二篇改成明天早上发布" }
  }
);

const originalFetch = globalThis.fetch;
let controlRequest: { url: string; init?: RequestInit } | null = null;
globalThis.fetch = async (input, init) => {
  controlRequest = { url: String(input), init };
  return Response.json({
    task_id: "task-internal",
    action: "CONFIRM",
    status: "CONFIRMED",
    confirmation_state: "CONFIRMED",
    task_version: 8,
    confirmation_version: 2,
    confirmed_version: 2,
    idempotent: false,
    resume_queued: true,
    requires_new_compilation: false
  });
};
try {
  const response = await agentService.controlSemanticConfirmation(
    "token",
    "task/internal",
    control
  );
  assert.equal(response.resume_queued, true);
  assert.equal(controlRequest?.url, "/agent-api/api/v1/agent/tasks/task%2Finternal/semantic-confirmation");
  assert.equal(controlRequest?.init?.method, "POST");
  assert.equal(
    controlRequest?.init?.headers
      && (controlRequest.init.headers as Record<string, string>).Authorization,
    "Bearer token"
  );
  assert.deepEqual(JSON.parse(String(controlRequest?.init?.body)), control);

  globalThis.fetch = async () => new Response(JSON.stringify({ detail: "internal stale detail" }), {
    status: 409,
    headers: { "Content-Type": "application/json" }
  });
  await assert.rejects(
    agentService.controlSemanticConfirmation("token", "task-internal", control),
    (caught: unknown) => caught instanceof AgentApiError && caught.status === 409
  );
} finally {
  globalThis.fetch = originalFetch;
}

const latest = selectLatestSemanticConfirmationEvents([
  event({ sequence: 10 }),
  event({
    activity_id: "activity-confirmation-2",
    sequence: 11,
    safe_payload: {
      ...event().safe_payload,
      confirmation_id: "confirmation-new-version",
      confirmation_version: 3,
      task_version: 12
    }
  }),
  event({
    activity_id: "ordinary-progress",
    activity_type: "PUBLISHING",
    status: "IN_PROGRESS",
    sequence: 12
  })
]);
assert.equal(latest.length, 1);
assert.equal(projectSemanticConfirmation(latest[0]!)?.confirmation_version, 3);
assert.equal(semanticConfirmationErrorMessage(409), "这项安排已经发生变化，请以最新版本为准。");
assert.equal(semanticConfirmationErrorMessage(500), "确认请求暂时没有完成，请稍后重试。");
