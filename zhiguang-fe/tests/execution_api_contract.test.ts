import assert from "node:assert/strict";
import { assistantService } from "../src/services/assistantService";
import { executionService, waitForExecution } from "../src/services/executionService";

const executionId = "execution-1";
const calls: string[] = [];
let statusReads = 0;

const running = {
  execution_id: executionId,
  task_id: "task-1",
  plan_id: "plan-1",
  status: "RUNNING",
  current_step: "GENERATE_CONTENT",
  progress: 0,
  total_steps: 1,
  completed_steps: 0,
  created_at: "2026-08-10T00:00:00Z",
  updated_at: "2026-08-10T00:00:00Z"
};

const completed = {
  ...running,
  status: "COMPLETED",
  progress: 1,
  completed_steps: 1
};

const step = {
  step_execution_id: "step-execution-1",
  step_id: "GENERATE_CONTENT",
  capability: "GENERATE_CONTENT",
  status: "COMPLETED",
  retry_count: 0,
  error_code: "",
  error_message: "",
  started_at: "2026-08-10T00:00:00Z",
  completed_at: "2026-08-10T00:00:01Z"
};

const event = {
  event_id: "event-1",
  execution_id: executionId,
  event_type: "STEP_COMPLETED",
  step_id: "GENERATE_CONTENT",
  timestamp: "2026-08-10T00:00:01Z",
  payload: {}
};

const originalFetch = globalThis.fetch;
globalThis.fetch = async (input, init) => {
  const url = String(input);
  calls.push(`${init?.method ?? "GET"} ${url}`);

  if (url.includes("/conversations/conversation-1/messages")) {
    return Response.json({
      run_id: "run-1",
      conversation_id: "conversation-1",
      status: "RUNNING",
      events_url: "/api/v1/assistant/runs/run-1/events",
      execution_id: executionId,
      execution_events_url: `/api/v1/executions/${executionId}/events`,
      replayed: false
    });
  }
  if (url.endsWith("/executions?limit=30")) {
    return Response.json({ items: [running], next_cursor: null });
  }
  if (url.endsWith(`/executions/${executionId}/stream`)) {
    return new Response(`event: STEP_COMPLETED\ndata: ${JSON.stringify(event)}\n\n`, {
      headers: { "Content-Type": "text/event-stream" }
    });
  }
  if (url.endsWith(`/executions/${executionId}/steps`)) {
    return Response.json({ execution_id: executionId, steps: statusReads > 1 ? [step] : [] });
  }
  if (url.endsWith(`/executions/${executionId}/events`)) {
    return Response.json({ execution_id: executionId, events: statusReads > 1 ? [event] : [] });
  }
  if (url.endsWith(`/executions/${executionId}`)) {
    statusReads += 1;
    return Response.json(statusReads > 1 ? completed : running);
  }
  throw new Error(`Unexpected request: ${url}`);
};

try {
  const accepted = await assistantService.send("token", "conversation-1", "写一篇 Runtime 文章");
  assert.equal(accepted.execution_id, executionId);

  const listed = await executionService.list("token");
  assert.equal(listed.items[0]?.execution_id, executionId);

  const updates: string[] = [];
  const events: string[] = [];
  const result = await waitForExecution(
    "token",
    executionId,
    snapshot => updates.push(snapshot.status),
    received => events.push(received.event_type)
  );
  assert.equal(result.status, "COMPLETED");
  assert.deepEqual(events, ["STEP_COMPLETED"]);
  assert.deepEqual(result.steps?.map(item => item.capability), ["GENERATE_CONTENT"]);
  assert.deepEqual(result.events?.map(item => item.event_type), ["STEP_COMPLETED"]);
  assert.ok(updates.includes("RUNNING"));
  assert.ok(updates.includes("COMPLETED"));
  assert.ok(calls.some(call => call.includes(`/executions/${executionId}/stream`)));
  assert.ok(calls.some(call => call.includes(`/executions/${executionId}/steps`)));
  assert.ok(calls.some(call => call.includes(`/executions/${executionId}/events`)));
} finally {
  globalThis.fetch = originalFetch;
}
