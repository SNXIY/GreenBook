import assert from "node:assert/strict";
import {
  projectAgentRunToUserFacingInteraction,
  projectAgentRunArtifactsToUserFacingInteractions,
  projectPendingApprovalFallback,
  approvalPresentation,
  projectRunActivity,
  projectTargetClarification,
  isTargetClarificationResolved,
  projectAgentMessageToUserFacingInteractions,
  dedupeTerminalAgentMessages,
  projectUserFacingInteractionPart,
  projectUserFacingInteraction,
  toUserFacingResult,
  formatUserFacingScheduleTime,
  userFacingMessage
} from "../src/components/agent/userFacingResult";
import {
  canSubmitNaturalLanguage,
  isComposerDisabled
} from "../src/components/agent/agentComposerState";
import type {
  AgentExecutionResultPart,
  AgentMessage,
  AgentResultArtifact,
  AgentRun,
  AgentUserFacingInteractionPart
} from "../src/types/agent";

const failedMessage = (executionId: string): AgentMessage => ({
  message_id: `message-${executionId}`,
  role: "assistant",
  content: "这次没有完成",
  run_id: executionId,
  execution_id: executionId,
  created_at: "2026-08-14T00:00:00Z",
  parts: [{
    type: "execution_result",
    execution: {
      execution_id: executionId,
      task_id: "task-stable",
      status: "FAILED",
      steps: [{ goal_id: "g4", capability: "GENERATE_CONTENT", status: "FAILED" }]
    },
    artifacts: [],
    next_actions: []
  }]
});

assert.equal(
  dedupeTerminalAgentMessages([
    failedMessage("run-1"),
    failedMessage("run-2")
  ]).length,
  1
);

const partialScheduleFailurePart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-1",
    task_id: "task-1",
    status: "FAILED",
    summary: "Generate a draft post about a new community topic",
    steps: [
      {
        step_id: "GENERATE_CONTENT",
        label: "Generate a draft post about a new community topic",
        status: "COMPLETED"
      },
      {
        step_id: "SCHEDULE_PUBLISH",
        label: "Schedule publish",
        status: "FAILED"
      }
    ]
  },
  artifacts: [
    {
      type: "POST_DRAFT",
      artifact_id: "artifact-draft-1",
      resource_type: "DRAFT",
      resource_id: "draft-1",
      title: "社区学习路线图：从基础到实践的 5 个阶段",
      summary: "从基础知识到项目实践的学习路线。"
    },
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-schedule-1",
      resource_type: "SCHEDULE",
      resource_id: "schedule-1",
      run_at: "2026-08-14T00:00:00.000Z",
      status: "FAILED",
      payload: { draft_id: "draft-1" }
    }
  ],
  schedule: {
    schedule_id: "schedule-1",
    run_at: "2026-08-14T00:00:00.000Z",
    status: "FAILED"
  },
  next_actions: []
};

const partialScheduleFailure = toUserFacingResult(partialScheduleFailurePart);

assert.equal(partialScheduleFailure.status, "PARTIAL_SUCCESS");
assert.equal(partialScheduleFailure.title, "帖子已经准备好了，但发布安排没有成功");
assert.deepEqual(
  partialScheduleFailure.actions.map(action => action.label),
  ["查看草稿"]
);
assert.ok(partialScheduleFailure.actions.every(action => action.kind === "link"));
assert.equal(partialScheduleFailure.hint, "可以继续直接告诉我下一步怎么调整。");
assert.deepEqual(
  partialScheduleFailure.activity.map(item => item.label),
  ["内容已生成", "未能安排发布时间"]
);

const compoundScheduledResult = toUserFacingResult({
  type: "execution_result",
  execution: {
    execution_id: "execution-compound",
    status: "COMPLETED",
    steps: [
      { step_id: "GENERATE_CONTENT", status: "COMPLETED" },
      { step_id: "SCHEDULE_PUBLISH", status: "COMPLETED" }
    ]
  },
  artifacts: [
    {
      type: "POST_DRAFT",
      artifact_id: "artifact-compound-draft",
      resource_type: "DRAFT",
      resource_id: "draft-compound",
      title: "一篇可发布的内容",
      summary: "正文预览"
    },
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-compound-schedule",
      resource_type: "SCHEDULE",
      resource_id: "schedule-compound",
      run_at: "2026-08-13T05:10:00.000Z",
      timezone: "Asia/Shanghai",
      status: "SCHEDULED",
      payload: { draft_id: "draft-compound" }
    }
  ],
  schedule: {
    schedule_id: "schedule-compound",
    run_at: "2026-08-13T05:10:00.000Z",
    status: "SCHEDULED"
  },
  next_actions: []
});
assert.equal(compoundScheduledResult.type, "SCHEDULED_POST");
assert.equal(compoundScheduledResult.draft?.title, "一篇可发布的内容");
assert.equal(compoundScheduledResult.schedule?.scheduleId, "schedule-compound");
assert.equal(compoundScheduledResult.schedule?.status, "SCHEDULED");

const verifyingResult = toUserFacingResult({
  type: "execution_result",
  execution: {
    execution_id: "hidden-execution",
    task_id: "hidden-task",
    status: "FAILED",
    business_projection: {
      state: "VERIFYING_RESULT",
      message: "正在确认操作结果，请不要重复操作。",
      visible: true,
      entities: [],
      actions: [],
      completed_count: 0,
      processing_count: 0,
      failed_count: 0,
      needs_action_count: 0
    }
  },
  artifacts: [],
  next_actions: []
});
assert.equal(verifyingResult.status, "IN_PROGRESS");
assert.notEqual(verifyingResult.type, "TASK_FAILED");

const supersededPart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "superseded-execution",
    task_id: "superseded-task",
    status: "SUPERSEDED",
    business_projection: {
      state: null,
      message: "",
      visible: false,
      entities: [],
      actions: [],
      completed_count: 0,
      processing_count: 0,
      failed_count: 0,
      needs_action_count: 0
    }
  },
  artifacts: [],
  next_actions: []
};
assert.deepEqual(projectAgentMessageToUserFacingInteractions([supersededPart]), []);

const multiGoalPart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-multi-goal",
    task_id: "task-multi-goal",
    status: "COMPLETED",
    steps: [
      { step_id: "goal-a:1", capability: "GENERATE_CONTENT", status: "COMPLETED" },
      { step_id: "goal-a:2", capability: "SCHEDULE_PUBLISH", status: "COMPLETED" },
      { step_id: "goal-b:1", capability: "GENERATE_CONTENT", status: "COMPLETED" },
      { step_id: "goal-b:2", capability: "SCHEDULE_PUBLISH", status: "COMPLETED" }
    ]
  },
  // Completion order is intentionally interleaved: B's schedule is before A's.
  artifacts: [
    {
      type: "POST_DRAFT",
      artifact_id: "artifact-draft-a",
      step_id: "goal-a:1",
      resource_type: "DRAFT",
      resource_id: "draft-a",
      title: "目标 A 内容",
      summary: "目标 A 摘要"
    },
    {
      type: "POST_DRAFT",
      artifact_id: "artifact-draft-b",
      step_id: "goal-b:1",
      resource_type: "DRAFT",
      resource_id: "draft-b",
      title: "目标 B 内容",
      summary: "目标 B 摘要"
    },
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-schedule-b",
      step_id: "goal-b:2",
      resource_type: "SCHEDULE",
      resource_id: "schedule-b",
      draft_id: "draft-b",
      run_at: "2026-08-14T07:00:00Z",
      timezone: "Asia/Shanghai",
      status: "SCHEDULED"
    },
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-schedule-a",
      step_id: "goal-a:2",
      resource_type: "SCHEDULE",
      resource_id: "schedule-a",
      draft_id: "draft-a",
      run_at: "2026-08-13T05:25:00Z",
      timezone: "Asia/Shanghai",
      status: "SCHEDULED"
    }
  ],
  next_actions: []
};
const multiGoalInteractions = projectAgentMessageToUserFacingInteractions([multiGoalPart]);
assert.equal(multiGoalInteractions.length, 1);
assert.equal(multiGoalInteractions[0].kind, "RESULT_GROUP");
if (multiGoalInteractions[0].kind === "RESULT_GROUP") {
  assert.equal(multiGoalInteractions[0].group.items.length, 2);
  const byDraft = new Map(
    multiGoalInteractions[0].group.items.map(item => [item.result.draft?.draftId, item.result])
  );
  assert.equal(byDraft.get("draft-a")?.schedule?.scheduleId, "schedule-a");
  assert.equal(byDraft.get("draft-b")?.schedule?.scheduleId, "schedule-b");
  assert.equal(multiGoalInteractions[0].group.status, "SUCCESS");
}
assert.equal(
  formatUserFacingScheduleTime("2026-08-13T05:25:00Z", "Asia/Shanghai"),
  "8/13 13:25"
);
assert.equal(
  formatUserFacingScheduleTime("2026-08-13T13:25:00+08:00", "Asia/Shanghai"),
  "8/13 13:25"
);

const partialMultiGoalInteractions = projectAgentMessageToUserFacingInteractions([{
  ...multiGoalPart,
  execution: {
    ...multiGoalPart.execution,
    status: "FAILED",
    steps: multiGoalPart.execution.steps?.map(step =>
      step.step_id === "goal-b:2" ? { ...step, status: "FAILED" } : step
    )
  },
  artifacts: multiGoalPart.artifacts.map(artifact =>
    artifact.resource_id === "schedule-b"
      ? { ...artifact, status: "FAILED" }
      : artifact
  )
}]);
assert.equal(partialMultiGoalInteractions[0].kind, "RESULT_GROUP");
if (partialMultiGoalInteractions[0].kind === "RESULT_GROUP") {
  assert.equal(partialMultiGoalInteractions[0].group.status, "PARTIAL_SUCCESS");
  const byDraft = new Map(
    partialMultiGoalInteractions[0].group.items.map(item => [item.result.draft?.draftId, item.result])
  );
  assert.equal(byDraft.get("draft-a")?.status, "SUCCESS");
  assert.equal(byDraft.get("draft-b")?.status, "PARTIAL_SUCCESS");
}

const partialWithoutScheduleArtifact = projectAgentMessageToUserFacingInteractions([{
  ...multiGoalPart,
  execution: {
    ...multiGoalPart.execution,
    status: "FAILED",
    steps: multiGoalPart.execution.steps?.map(step =>
      step.step_id === "goal-b:2" ? { ...step, status: "FAILED" } : step
    )
  },
  artifacts: multiGoalPart.artifacts.filter(artifact => artifact.resource_id !== "schedule-b")
}]);
assert.equal(partialWithoutScheduleArtifact[0].kind, "RESULT_GROUP");
if (partialWithoutScheduleArtifact[0].kind === "RESULT_GROUP") {
  const byDraft = new Map(
    partialWithoutScheduleArtifact[0].group.items.map(item => [item.result.draft?.draftId, item.result])
  );
  assert.equal(byDraft.get("draft-a")?.status, "SUCCESS");
  assert.equal(byDraft.get("draft-b")?.status, "PARTIAL_SUCCESS");
}

const failedBeforeScheduleProjection = toUserFacingResult({
  ...partialScheduleFailurePart,
  artifacts: partialScheduleFailurePart.artifacts.slice(0, 1),
  schedule: undefined
});
assert.equal(failedBeforeScheduleProjection.status, "PARTIAL_SUCCESS");
assert.equal(failedBeforeScheduleProjection.title, "帖子已经准备好了，但发布安排没有成功");
assert.deepEqual(
  failedBeforeScheduleProjection.actions.map(action => action.label),
  ["查看草稿"]
);

const defaultUserFacingFields = {
  status: partialScheduleFailure.status,
  title: partialScheduleFailure.title,
  summary: partialScheduleFailure.summary,
  draft: partialScheduleFailure.draft,
  schedule: partialScheduleFailure.schedule,
  actions: partialScheduleFailure.actions,
  activity: partialScheduleFailure.activity
};
const defaultText = JSON.stringify(defaultUserFacingFields);
assert.doesNotMatch(defaultText, /execution[_ -]?id|task[_ -]?id|plan[_ -]?id|g3:1|2\/4|50%/i);
assert.match(JSON.stringify(partialScheduleFailure.technical), /execution-1/);

const filtered = userFacingMessage(
  "Generate a draft post about a new community topic. Overall user objective context..."
);
assert.doesNotMatch(filtered, /Generate a draft post|Overall user objective context/i);
assert.doesNotMatch(filtered, /Runtime|Execution|TaskPlan|MCP/i);
const reflected = userFacingMessage(
  "The search for posts about Agent returned 18 results, and detailed content was retrieved for three representative posts. The user's goal is satisfied."
);
assert.doesNotMatch(reflected, /goal is satisfied|search returned|successfully completed/i);

const synthesisPart: AgentUserFacingInteractionPart = {
  type: "user_facing_interaction",
  interaction: {
    kind: "SYNTHESIS_RESULT",
    synthesis: {
      title: "检索内容综合整理",
      intro: "找到 18 篇相关内容，重点阅读了 3 篇。",
      total_matched: 18,
      selected_count: 3,
      read_count: 3,
      sources: [
        {
          resource_id: "post-a",
          title: "工作流实践",
          summary: "先拆解目标，再逐步执行。",
          href: "/post/post-a",
          read_status: "FULL",
          source_refs: ["source-1"]
        },
        {
          resource_id: "post-b",
          title: "工具调用设计",
          summary: "把工具能力拆分为可验证的步骤。",
          href: "/post/post-b",
          read_status: "PARTIAL",
          source_refs: ["source-2"]
        },
        {
          resource_id: "post-c",
          title: "状态管理经验",
          summary: "记录执行状态并评估结果。",
          href: "/post/post-c",
          read_status: "METADATA_ONLY",
          source_refs: ["source-3"]
        }
      ],
      common_patterns: [
        {
          title: "先拆解目标",
          explanation: "多篇内容都先把目标拆成可执行步骤。",
          source_refs: ["source-1", "source-2"]
        },
        {
          title: "未被多来源支持的观点",
          explanation: "只有一篇内容提到。",
          source_refs: ["source-3"]
        }
      ],
      conclusion: "这些内容共同强调可执行的步骤和可验证的过程。"
    }
  }
};
const synthesisInteraction = projectUserFacingInteractionPart(synthesisPart);
assert.equal(synthesisInteraction.kind, "SYNTHESIS_RESULT");
if (synthesisInteraction.kind === "SYNTHESIS_RESULT") {
  assert.equal(synthesisInteraction.synthesis.totalMatched, 18);
  assert.equal(synthesisInteraction.synthesis.selectedCount, 3);
  assert.equal(synthesisInteraction.synthesis.sources.length, 3);
  assert.equal(synthesisInteraction.synthesis.commonPatterns.length, 1);
  assert.equal(synthesisInteraction.synthesis.sources[0].href, "/post/post-a");
  assert.equal(synthesisInteraction.synthesis.sources[1].readStatus, "PARTIAL");
  assert.equal(synthesisInteraction.synthesis.sources[2].readStatus, "METADATA_ONLY");
  assert.doesNotMatch(JSON.stringify(synthesisInteraction), /execution_id|task_id|plan_id/i);
}

const retrievalRun = {
  status: "RUNNING",
  steps: [
    {
      step_id: "search",
      kind: "TOOL",
      tool_name: "community.search_public_posts",
      label: "community.search_public_posts",
      status: "COMPLETED",
      capabilities: ["SEARCH_COMMUNITY"]
    },
    {
      step_id: "read",
      kind: "TOOL",
      tool_name: "community.get_post",
      label: "community.get_post",
      status: "COMPLETED",
      capabilities: ["GET_POST_DETAIL"]
    }
  ]
} as AgentRun;
const retrievalActivity = projectRunActivity(retrievalRun);
assert.deepEqual(
  retrievalActivity.map(item => item.label),
  ["已查找相关内容", "已阅读代表性内容", "正在整理共同观点…"]
);
assert.doesNotMatch(JSON.stringify(retrievalActivity), /community\.search_public_posts|community\.get_post|Tool call|Goal/i);

const defensiveRefProjection = projectUserFacingInteractionPart({
  type: "user_facing_interaction",
  interaction: {
    kind: "SYNTHESIS_RESULT",
    synthesis: {
      title: "内容总结",
      sources: [
        { title: "内容一", source_refs: ["source-1"] },
        { title: "内容二", source_refs: ["source-2"] }
      ],
      common_patterns: [{
        title: "共同点",
        explanation: "source-1 和 source-2 都提到这一点。",
        source_refs: ["source-1", "source-2"]
      }],
      differences: [{
        title: "不足以比较",
        explanation: "可能分为 3 个阶段。",
        source_refs: ["source-1", "source-2"]
      }],
      conclusion: "综合来看，source-1 和 source-2 支持这个结论。"
    }
  }
});
assert.equal(defensiveRefProjection.kind, "SYNTHESIS_RESULT");
if (defensiveRefProjection.kind === "SYNTHESIS_RESULT") {
  assert.deepEqual(defensiveRefProjection.synthesis.commonPatterns, []);
  assert.deepEqual(defensiveRefProjection.synthesis.differences, []);
  assert.doesNotMatch(defensiveRefProjection.synthesis.conclusion || "", /source-1|source-2/i);
  assert.deepEqual(defensiveRefProjection.synthesis.sources[0].sourceRefs, ["source-1"]);
}

const insufficientSynthesis = projectUserFacingInteractionPart({
  type: "user_facing_interaction",
  interaction: {
    kind: "SYNTHESIS_RESULT",
    synthesis: {
      title: "检索内容综合整理",
      evidence_note: "目前只取得 1 篇完整内容，还不足以可靠总结共同点。",
      sources: [{ title: "唯一正文", source_refs: ["source-1"] }],
      common_patterns: [{
        title: "不应显示",
        explanation: "只有一个来源。",
        source_refs: ["source-1"]
      }]
    }
  }
});
assert.equal(insufficientSynthesis.kind, "SYNTHESIS_RESULT");
if (insufficientSynthesis.kind === "SYNTHESIS_RESULT") {
  assert.deepEqual(insufficientSynthesis.synthesis.commonPatterns, []);
  assert.match(insufficientSynthesis.synthesis.evidenceNote || "", /不足以可靠/);
}

const pureSearchInteraction = projectUserFacingInteractionPart({
  type: "user_facing_interaction",
  interaction: {
    kind: "QUERY_RESULT",
    result: {
      type: "SEARCH_RESULTS",
      status: "SUCCESS",
      title: "找到 8 篇相关内容",
      search: {
        count: 8,
        items: [{
          id: "post-search-1",
          title: "真实搜索结果",
          summary: "来自社区公开内容。",
          href: "/post/post-search-1"
        }]
      }
    }
  }
});
assert.equal(pureSearchInteraction.kind, "QUERY_RESULT");
if (pureSearchInteraction.kind === "QUERY_RESULT") {
  assert.equal(pureSearchInteraction.result.search?.count, 8);
  assert.equal(pureSearchInteraction.result.search?.items[0].href, "/post/post-search-1");
}

const revisedPart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-revise",
    task_id: "task-revise",
    status: "COMPLETED",
    steps: [{ step_id: "REVISE_DRAFT", status: "COMPLETED" }]
  },
  artifacts: [{
    type: "POST_DRAFT",
    artifact_id: "artifact-revise",
    resource_type: "DRAFT",
    resource_id: "draft-revise",
    title: "更新后的标题",
    summary: "更新后的内容摘要"
  }],
  next_actions: []
};
const revisedInteraction = projectUserFacingInteraction(revisedPart);
assert.equal(revisedInteraction.kind, "CHANGE_CONFIRMATION");
if (revisedInteraction.kind === "CHANGE_CONFIRMATION") {
  assert.equal(revisedInteraction.change.changeType, "CONTENT");
  assert.equal(revisedInteraction.change.summary, "内容已更新");
  assert.deepEqual(revisedInteraction.change.navigation?.map(link => link.label), ["查看草稿"]);
  assert.doesNotMatch(JSON.stringify(revisedInteraction.change), /execution|task|plan|goal|step/i);
}

const scheduleChangePart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-schedule-change",
    task_id: "task-schedule-change",
    status: "COMPLETED",
    steps: [{ step_id: "MANAGE_SCHEDULE", status: "COMPLETED" }]
  },
  artifacts: [{
    type: "POST_DRAFT",
    artifact_id: "artifact-schedule-draft",
    resource_type: "DRAFT",
    resource_id: "draft-schedule",
    title: "需要调整时间的内容"
  }, {
    type: "PUBLICATION_SCHEDULE",
    artifact_id: "artifact-schedule-change",
    resource_type: "SCHEDULE",
    resource_id: "schedule-change",
    run_at: "2026-08-15T02:00:00.000Z",
    status: "SCHEDULED",
    payload: { draft_id: "draft-schedule" }
  }],
  schedule: {
    schedule_id: "schedule-change",
    run_at: "2026-08-15T02:00:00.000Z",
    status: "SCHEDULED"
  },
  next_actions: []
};
const scheduleChangeInteraction = projectUserFacingInteraction(scheduleChangePart);
assert.equal(scheduleChangeInteraction.kind, "CHANGE_CONFIRMATION");
if (scheduleChangeInteraction.kind === "CHANGE_CONFIRMATION") {
  assert.equal(scheduleChangeInteraction.change.changeType, "SCHEDULE");
  assert.equal(scheduleChangeInteraction.change.summary, "发布时间已更新");
}

const queryPart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-query",
    status: "COMPLETED",
    steps: [{ step_id: "SEARCH_COMMUNITY", status: "COMPLETED" }]
  },
  artifacts: [{
    type: "SEARCH_RESULT",
    artifact_id: "artifact-search",
    resource_type: "SEARCH_RESULT",
    payload: {
      items: [{ post_id: "post-1", title: "Agent 设计实践", summary: "社区内容" }]
    }
  }],
  next_actions: []
};
const queryInteraction = projectUserFacingInteraction(queryPart);
assert.equal(queryInteraction.kind, "QUERY_RESULT");
if (queryInteraction.kind === "QUERY_RESULT") {
  assert.equal(queryInteraction.result.search?.items[0].href, "/post/post-1");
}

const analysisPart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-analysis",
    status: "COMPLETED",
    steps: [{ step_id: "ANALYZE_CONTENT_PATTERNS", status: "COMPLETED" }]
  },
  artifacts: [{
    type: "ANALYSIS_REPORT",
    artifact_id: "artifact-analysis",
    resource_type: "ANALYSIS_REPORT",
    payload: { totalPublished: 3, highlight: "技术类内容表现更好" }
  }],
  next_actions: []
};
assert.equal(projectUserFacingInteraction(analysisPart).kind, "ANALYSIS_RESULT");

const failedPart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-failed",
    status: "FAILED",
    steps: [{ step_id: "GENERATE_CONTENT", status: "FAILED" }]
  },
  artifacts: [],
  next_actions: []
};
const failureInteraction = projectUserFacingInteraction(failedPart);
assert.equal(failureInteraction.kind, "FAILURE_RESULT");
if (failureInteraction.kind === "FAILURE_RESULT") {
  assert.deepEqual(failureInteraction.result.actions, []);
  assert.match(failureInteraction.result.hint || "", /直接告诉我/);
}

const approvalRun: AgentRun = {
  run_id: "run-approval",
  execution_id: "execution-approval",
  conversation_id: "conversation-approval",
  goal: "发布内容",
  status: "WAITING_APPROVAL",
  execution_path: "ORCHESTRATED",
  workload_lane: "WRITE",
  trace_id: "trace-approval",
  budget: { model_calls: 0, max_model_calls: 1, tool_calls: 0, max_tool_calls: 1, replan_count: 0, max_replans: 1 },
  timing: { queue_ms: 0, model_ms: 0, tool_ms: 0, dependency_wait_ms: 0, total_ms: 0 },
  task_ledger: {},
  progress_ledger: {},
  artifacts: [{
    type: "POST_DRAFT",
    artifact_id: "artifact-approval",
    resource_type: "DRAFT",
    resource_id: "draft-approval",
    title: "待发布内容"
  }],
  partial_results: {},
  approval: {
    approval_id: "approval-1",
    action: "PUBLISH_NOW",
    status: "PENDING",
    description: "这篇内容将公开发布。",
    preview: { draft_id: "draft-approval" },
    expires_at: "2026-08-14T00:00:00.000Z",
    expected_run_version: 1
  },
  steps: [],
  created_at: "2026-08-13T00:00:00.000Z",
  updated_at: "2026-08-13T00:00:00.000Z"
};
const approvalInteraction = projectAgentRunToUserFacingInteraction(approvalRun);
assert.equal(approvalInteraction?.kind, "APPROVAL_REQUEST");
if (approvalInteraction?.kind === "APPROVAL_REQUEST") {
  assert.equal(approvalInteraction.approval.actionTitle, "准备立即发布");
  assert.equal(approvalInteraction.approval.confirmLabel, "确认发布");
  assert.equal(approvalInteraction.approval.canConfirm, true);
  assert.equal(approvalInteraction.approval.canReject, true);
}

const mixedApprovalRun: AgentRun = {
  ...approvalRun,
  artifacts: [
    {
      ...approvalRun.artifacts[0],
      step_id: "goal-a:1",
      resource_id: "draft-a",
      title: "需要确认的内容"
    },
    {
      type: "POST_DRAFT",
      artifact_id: "artifact-mixed-b",
      step_id: "goal-b:1",
      resource_type: "DRAFT",
      resource_id: "draft-b",
      title: "已安排的内容"
    },
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-mixed-schedule-b",
      step_id: "goal-b:2",
      resource_type: "SCHEDULE",
      resource_id: "schedule-b",
      draft_id: "draft-b",
      run_at: "2026-08-14T07:00:00Z",
      timezone: "Asia/Shanghai",
      status: "SCHEDULED"
    }
  ],
  steps: [
    { ...approvalRun.steps[0], step_id: "goal-a:1", label: "生成内容", status: "COMPLETED" },
    { ...approvalRun.steps[0], step_id: "goal-b:1", label: "生成内容", status: "COMPLETED" },
    { ...approvalRun.steps[0], step_id: "goal-b:2", label: "安排发布时间", status: "COMPLETED" }
  ]
};
const mixedApprovalResults = projectAgentRunArtifactsToUserFacingInteractions(mixedApprovalRun);
assert.equal(mixedApprovalResults.length, 1);
assert.equal(mixedApprovalResults[0].kind, "RESULT_GROUP");
if (mixedApprovalResults[0].kind === "RESULT_GROUP") {
  assert.equal(mixedApprovalResults[0].group.items.length, 2);
  const mixedByDraft = new Map(
    mixedApprovalResults[0].group.items.map(item => [item.result.draft?.draftId, item.result])
  );
  assert.equal(mixedByDraft.get("draft-a")?.status, "SUCCESS");
  assert.equal(mixedByDraft.get("draft-b")?.schedule?.scheduleId, "schedule-b");
}
assert.equal(projectAgentRunToUserFacingInteraction(mixedApprovalRun)?.kind, "APPROVAL_REQUEST");

assert.equal(isComposerDisabled("READY", true), false);
assert.equal(canSubmitNaturalLanguage("标题再短一点", "READY", true), true);
assert.equal(isComposerDisabled("SUBMITTING", true), true);
assert.equal(canSubmitNaturalLanguage("继续修改", "SUBMITTING", true), false);
assert.equal(isComposerDisabled("READY", false), true);

const scheduledApproval = {
  ...approvalRun,
  artifacts: [
    { ...approvalRun.artifacts[0], summary: "草稿已经准备好，包含并发优化实践。" },
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-approval-schedule",
      resource_type: "SCHEDULE",
      resource_id: "schedule-approval",
      run_at: "2026-08-13T05:10:00.000Z",
      timezone: "Asia/Shanghai",
      status: "PENDING"
    }
  ],
  approval: {
    ...approvalRun.approval,
    action: "PUBLISH_SCHEDULED"
  }
} as AgentRun;
const scheduledApprovalCopy = approvalPresentation(scheduledApproval);
assert.equal(scheduledApprovalCopy.actionTitle, "准备按计划发布");
assert.match(scheduledApprovalCopy.plannedTime || "", /13:10/);
assert.match(scheduledApprovalCopy.consequence, /13:10/);
assert.match(scheduledApprovalCopy.draftPreview || "", /草稿已经准备好/);

const scopedApprovalWithoutArtifact = {
  ...approvalRun,
  artifacts: [],
  approval: {
    ...approvalRun.approval,
    action: "publication.schedule",
    preview: {
      draft_id: "draft-scoped",
      target_title: "Scoped approval target",
      run_at: "2026-08-14T07:00:00Z",
      timezone: "Asia/Shanghai"
    }
  }
} as AgentRun;
const scopedApprovalCopy = approvalPresentation(scopedApprovalWithoutArtifact);
assert.equal(scopedApprovalCopy.resourceTitle, "Scoped approval target");
assert.equal(scopedApprovalCopy.actionTitle, "准备按计划发布");
assert.match(scopedApprovalCopy.plannedTime || "", /15:00/);

const targetlessApprovalCopy = approvalPresentation({
  ...scopedApprovalWithoutArtifact,
  approval: {
    ...scopedApprovalWithoutArtifact.approval,
    preview: { draft_id: "draft-targetless" }
  }
} as AgentRun);
assert.equal(targetlessApprovalCopy.resourceTitle, "待确认草稿");
assert.notEqual(targetlessApprovalCopy.resourceTitle, "这篇内容");

const missingApproval = projectPendingApprovalFallback({
  ...approvalRun,
  approval: undefined
}, undefined);
assert.equal(missingApproval?.kind, "APPROVAL_REQUEST");
if (missingApproval?.kind === "APPROVAL_REQUEST") {
  assert.equal(missingApproval.approval.canConfirm, false);
  assert.equal(missingApproval.approval.canReject, false);
  assert.match(missingApproval.approval.description, /暂时无法加载/);
}

const approvedRun = projectAgentRunToUserFacingInteraction({
  ...approvalRun,
  status: "RUNNING",
  approval: { ...approvalRun.approval, status: "APPROVED" }
});
assert.equal(approvedRun, null);

const cancelSchedulePart: AgentExecutionResultPart = {
  type: "execution_result",
  execution: {
    execution_id: "execution-cancel-schedule",
    status: "COMPLETED",
    steps: [{ step_id: "CANCEL_SCHEDULE", status: "COMPLETED" }]
  },
  artifacts: [{
    type: "POST_DRAFT",
    artifact_id: "artifact-cancel-draft",
    resource_type: "DRAFT",
    resource_id: "draft-cancel",
    title: "保留为草稿的内容"
  }, {
    type: "PUBLICATION_SCHEDULE",
    artifact_id: "artifact-cancel-schedule",
    resource_type: "SCHEDULE",
    resource_id: "schedule-cancel",
    run_at: "2026-08-15T02:00:00.000Z",
    status: "CANCELLED",
    payload: { draft_id: "draft-cancel" }
  }],
  schedule: {
    schedule_id: "schedule-cancel",
    run_at: "2026-08-15T02:00:00.000Z",
    status: "CANCELLED"
  },
  next_actions: []
};
const cancelInteraction = projectUserFacingInteraction(cancelSchedulePart);
assert.equal(cancelInteraction.kind, "CONTROL_CONFIRMATION");
if (cancelInteraction.kind === "CONTROL_CONFIRMATION") {
  assert.equal(cancelInteraction.control.changeType, "CANCEL");
  assert.equal(cancelInteraction.control.summary, "发布计划已取消");
}

const clarification = projectTargetClarification({
  type: "target_clarification",
  command: { command_id: "command-1", target: {} },
  candidates: [{
    identity: "draft-a",
    type: "DRAFT",
    label: "第一个草稿",
    status: "DRAFT",
    resource_id: "draft-a"
  }, {
    identity: "draft-b",
    type: "DRAFT",
    label: "Generate a draft post about…",
    status: "DRAFT",
    resource_id: "draft-b"
  }]
});
assert.equal(clarification.kind, "ASK_USER");
assert.equal(clarification.clarification.candidates[0].label, "第一个草稿");
assert.match(clarification.clarification.candidates[1].label, /未命名草稿/);
assert.doesNotMatch(JSON.stringify(clarification.clarification), /resource_id|command_id|Generate a draft post/i);

const clarificationMessage = (messageId: string, role: "user" | "assistant"): AgentMessage => ({
  message_id: messageId,
  role,
  content: role === "assistant" ? "请选择目标" : "选择：第一个目标",
  parts: role === "assistant" ? [{
    type: "target_clarification",
    command: { command_id: "command-resolved", target: {} },
    candidates: []
  }] : [],
  created_at: "2026-08-24T00:00:00.000Z"
});
assert.equal(
  isTargetClarificationResolved([clarificationMessage("clarify", "assistant")], 0),
  false
);
assert.equal(
  isTargetClarificationResolved([
    clarificationMessage("clarify", "assistant"),
    clarificationMessage("selection", "user")
  ], 0),
  true
);

type GoalFixture = {
  id: string;
  capability: string;
  status?: string;
};

const makeGoalProjectionPart = (
  executionStatus: string,
  goals: GoalFixture[],
  artifacts: AgentResultArtifact[]
): AgentExecutionResultPart => ({
  type: "execution_result",
  execution: {
    execution_id: "execution-dynamic-goals",
    task_id: "task-dynamic-goals",
    status: executionStatus,
    steps: goals.map(goal => ({
      step_id: `${goal.id}:1`,
      goal_id: goal.id,
      capability: goal.capability,
      status: goal.status || "COMPLETED"
    }))
  },
  artifacts,
  next_actions: []
});

const draftForGoal = (goalId: string, draftId: string): AgentResultArtifact => ({
  type: "POST_DRAFT",
  artifact_id: `artifact-${draftId}`,
  step_id: `${goalId}:1`,
  resource_type: "DRAFT",
  resource_id: draftId,
  title: `目标 ${goalId} 内容`,
  summary: `目标 ${goalId} 摘要`
});

const oneGoalProjection = projectAgentMessageToUserFacingInteractions([
  makeGoalProjectionPart("COMPLETED", [
    { id: "g-17", capability: "GENERATE_CONTENT" }
  ], [draftForGoal("g-17", "draft-17")])
]);
assert.equal(oneGoalProjection.length, 1);
assert.notEqual(oneGoalProjection[0].kind, "RESULT_GROUP");

const twoGoalProjection = projectAgentMessageToUserFacingInteractions([
  makeGoalProjectionPart("COMPLETED", [
    { id: "g-04", capability: "GENERATE_CONTENT" },
    { id: "g-92", capability: "GENERATE_CONTENT" }
  ], [
    draftForGoal("g-92", "draft-92"),
    draftForGoal("g-04", "draft-04")
  ])
]);
assert.equal(twoGoalProjection.length, 1);
assert.equal(twoGoalProjection[0].kind, "RESULT_GROUP");
if (twoGoalProjection[0].kind === "RESULT_GROUP") {
  assert.equal(twoGoalProjection[0].group.items.length, 2);
  assert.deepEqual(
    twoGoalProjection[0].group.items.map(item => item.result.draft?.draftId).sort(),
    ["draft-04", "draft-92"]
  );
}

const threeGoalProjection = projectAgentMessageToUserFacingInteractions([
  makeGoalProjectionPart("COMPLETED", [
    { id: "g-92", capability: "CANCEL_SCHEDULE" },
    { id: "g-17", capability: "SEARCH_COMMUNITY" },
    { id: "g-04", capability: "GENERATE_CONTENT" }
  ], [
    draftForGoal("g-04", "draft-04"),
    {
      type: "SEARCH_RESULT",
      artifact_id: "artifact-search-17",
      step_id: "g-17:1",
      resource_type: "SEARCH_RESULT",
      payload: { items: [{ id: "post-17", title: "真实查询结果" }] }
    }
  ])
]);
assert.equal(threeGoalProjection.length, 1);
assert.equal(threeGoalProjection[0].kind, "RESULT_GROUP");
if (threeGoalProjection[0].kind === "RESULT_GROUP") {
  assert.equal(threeGoalProjection[0].group.items.length, 3);
  assert.equal(
    threeGoalProjection[0].group.items.filter(item => item.result.type === "SEARCH_RESULTS").length,
    1
  );
  assert.equal(
    threeGoalProjection[0].group.items.filter(item => item.result.type === "GENERIC_RESULT").length,
    1
  );
}

const fiveGoalProjection = projectAgentMessageToUserFacingInteractions([
  makeGoalProjectionPart("WAITING_APPROVAL", [
    { id: "g-92", capability: "GENERATE_CONTENT" },
    { id: "g-04", capability: "SEARCH_COMMUNITY" },
    { id: "g-17", capability: "GENERATE_CONTENT" },
    { id: "g-31", capability: "PUBLISH_NOW", status: "WAITING_APPROVAL" },
    { id: "g-58", capability: "GENERATE_CONTENT", status: "FAILED" }
  ], [
    draftForGoal("g-92", "draft-92"),
    {
      type: "SEARCH_RESULT",
      artifact_id: "artifact-search-04",
      step_id: "g-04:1",
      resource_type: "SEARCH_RESULT",
      payload: { items: [{ id: "post-04", title: "查询结果" }] }
    },
    draftForGoal("g-17", "draft-17"),
    draftForGoal("g-31", "draft-31"),
    {
      type: "PUBLICATION_SCHEDULE",
      artifact_id: "artifact-schedule-17",
      step_id: "g-17:1",
      resource_type: "SCHEDULE",
      resource_id: "schedule-17",
      draft_id: "draft-17",
      status: "FAILED"
    }
  ])
]);
assert.equal(fiveGoalProjection.length, 1);
assert.equal(fiveGoalProjection[0].kind, "RESULT_GROUP");
if (fiveGoalProjection[0].kind === "RESULT_GROUP") {
  assert.equal(fiveGoalProjection[0].group.items.length, 5);
  assert.deepEqual(
    fiveGoalProjection[0].group.items.map(item => item.result.status).sort(),
    ["FAILED", "NEEDS_ACTION", "PARTIAL_SUCCESS", "SUCCESS", "SUCCESS"]
  );
}
