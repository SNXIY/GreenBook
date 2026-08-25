import assert from "node:assert/strict";
import { activityLabel, groupActivities } from "../src/components/agent/userActivityPresentation";
import {
  mergeUserActivityEvents,
  parseActivityFrame
} from "../src/services/userActivityService";
import type { UserActivityEvent } from "../src/types/userActivity";

const event = (overrides: Partial<UserActivityEvent> = {}): UserActivityEvent => ({
  activity_id: "activity-1",
  conversation_id: "conversation-1",
  task_id: "task-java",
  activity_type: "SCHEDULE_UPDATED",
  status: "COMPLETED",
  display_key: "activity.schedule.updated",
  safe_payload: {
    title: "Java interview",
    run_at: "2026-08-16T09:00:00+08:00",
    timezone: "Asia/Shanghai"
  },
  sequence: 2,
  created_at: "2026-08-15T00:00:00Z",
  terminal: true,
  ...overrides
});

const scheduleLabel = activityLabel(event());
assert.match(scheduleLabel, /发布时间已改为/);
assert.doesNotMatch(scheduleLabel, /schedule|tool|execution|task-java/i);

const reconcilingLabel = activityLabel(event({
  activity_type: "RESULT_UNKNOWN",
  status: "RESULT_UNKNOWN",
  safe_payload: {}
}));
assert.match(reconcilingLabel, /确认操作结果/);
assert.doesNotMatch(reconcilingLabel, /RESULT_UNKNOWN/i);

const groups = groupActivities([
  event({ activity_id: "java-search", sequence: 1, task_id: "task-java", activity_type: "SEARCH_STARTED", status: "IN_PROGRESS", safe_payload: {} }),
  event({ activity_id: "agent-create", sequence: 2, task_id: "task-agent", activity_type: "DRAFT_CREATING", status: "IN_PROGRESS", safe_payload: {} }),
  event({ activity_id: "java-draft", sequence: 3, task_id: "task-java", activity_type: "DRAFT_CREATED", status: "COMPLETED", safe_payload: { title: "Java interview" } })
]);
assert.equal(groups.length, 2);
assert.deepEqual(groups.map(group => group.events.length), [2, 1]);
assert.equal(groups[0]?.events[0]?.task_id, "task-java");
assert.equal(groups[1]?.events[0]?.task_id, "task-agent");

const frame = [
  "id: 9",
  "event: user_activity",
  `data: ${JSON.stringify(event({ activity_id: "activity-9", sequence: 9 }))}`,
  "",
  ""
].join("\n");
assert.equal(parseActivityFrame(frame)?.activity_id, "activity-9");
assert.equal(parseActivityFrame("event: legacy\ndata: {}\n\n"), null);

const merged = mergeUserActivityEvents(
  [event({ activity_id: "activity-2", sequence: 2 })],
  [event({ activity_id: "activity-2", sequence: 2 }), event({ activity_id: "activity-1", sequence: 1 })]
);
assert.deepEqual(merged.map(item => item.activity_id), ["activity-1", "activity-2"]);
