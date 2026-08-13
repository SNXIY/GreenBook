# Phase 8.1 Case 2 — Multi-turn Complex Revision

## Input

Turn 1:

`帮我创建一个AI Agent系列文章，第一篇介绍Agent架构，面向Java后端工程师，发布到GreenBook。`

Turn 2:

`我觉得方向不太好，改成面向刚入门AI Agent的开发者，增加LangGraph和MCP内容，不要太理论，加入实际代码案例，发布时间改成周五晚上8点。`

Turn 3:

`标题再调整一下，不要突出LangGraph，更强调企业级Agent架构设计。`

## Execution Trace

```text
Turn 1:
  Command -> existing-content GoalTree -> one Task
  -> Plan revision 1 -> GENERATE_CONTENT -> Creator artifact
  -> PUBLISH_NOW -> WAITING_APPROVAL

Turn 2:
  Conversation + Context -> TargetResolver -> same Task
  -> Goal revision / Plan revision 2
  -> Creator revision -> Java schedule update

Turn 3:
  Conversation + Context -> same Task
  -> Goal revision / Plan revision 3
  -> Creator title/content revision
```

The first three required turns did not create a second independent GreenBook Task. The first turn reached the real Creator Service and a persisted Java draft, the second turn revised the existing content and moved the schedule to Friday 20:00 Asia/Shanghai, and the third turn produced a concise enterprise-architecture title: `企业级AI Agent架构设计：从入门到实践`.

## Evidence

conversation_id: `d69c629e-f9ba-4e65-a3c8-65b6b61a5a6d`

run_id: `c665f5a4-7c19-4643-adbf-e86e20b7c499`, `cd44d679-9db9-44f7-a70e-28b3357291f4`, `405dc8ff-a95b-4319-8dbe-f23fc649129a`

execution_id: `d2775a6d-0824-4fc1-9c34-65a945a896ec`, `bc7165e4-aa5d-44af-983f-f59b2a1fe4e9`, `773b8068-2722-43c6-b8c9-17ef0e866d9d`

task_id: `73790329-0ff6-46ac-912a-9ef3b5c804ec` for all three turns

goal_id: final revision `g-revise-title-enterprise-agent-architecture-concise`; the durable task projection contains the preceding goal revisions

artifact_id: Creator artifacts `art_2ab81157326290b58b5a6194e6adb00b5d5bc27f8fbdbcead516fd8cbaee5ab8`, `art_0ff0d0f5cc2c21e6beb4e654afcf892e7eed90d51219744d2b1a49c3fdea1967`, `art_1f6e97655e2356d67a0b0915ea0269ca5a8773fd155845e25df89e708e53f6cb`; Java draft `345826212469411840`; schedule `345826647951413248`

plan revision: same Task; `goal_tree_version=5`, `plan_version=3` after the first three turns

## Result

**PARTIAL.** The intended three-turn behavior passed: one Task, context binding, Goal/Plan revisions, Creator revisions, and schedule update. A later English title-only follow-up against the same Task failed at the real Creator API with HTTP 422 (`CREATOR_REQUEST_REJECTED`).

## Problem

The Creator contract does not accept the narrow title-only revision payload emitted for the later follow-up. The runtime correctly kept the same Task and did not substitute fake content, but the external Creator handoff failed.

## Fix

No mock or fallback content was added. The existing Task/Goal revision and stale-completion protection were retained. The remaining fix is an explicit Creator contract for title-only revision (or a documented requirement to send the current body together with the title change), followed by another real three-turn run.

