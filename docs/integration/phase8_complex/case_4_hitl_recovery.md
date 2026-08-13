# Phase 8.1 Case 4 — Human Approval and Long-task Recovery

## Input

`帮我创建一个AI Agent系列文章，第一篇介绍Agent架构，面向Java后端工程师，发布到GreenBook。`

The final publication operation was intentionally held for approval.

## Execution Trace

```text
Frontend -> Agent API -> Command -> GoalTree -> Task
-> Plan (generate, then publish)
-> external Worker claim
-> Creator Service -> Creator artifact -> Java draft
-> PUBLISH_NOW policy gate
-> WAITING_APPROVAL
-> Worker stopped
-> Worker restarted and reclaimed durable state
-> APPROVE
-> checkpoint resume -> Java publication operation
-> COMPLETED
```

The Worker was stopped while the execution was `WAITING_APPROVAL`. After restart it resumed the existing Execution from its checkpoint. The Creator task and article were not generated again.

## Evidence

conversation_id: `d69c629e-f9ba-4e65-a3c8-65b6b61a5a6d`

run_id: `c665f5a4-7c19-4643-adbf-e86e20b7c499`

execution_id: `d2775a6d-0824-4fc1-9c34-65a945a896ec`

task_id: `73790329-0ff6-46ac-912a-9ef3b5c804ec`

goal_id: `g-generate-agent-architecture-article`, `g-publish-article-to-greenbook-now`

artifact_id: runtime draft `851441f7-cc09-4659-be4f-94815de0d18d`, Creator artifact `art_2ab81157326290b58b5a6194e6adb00b5d5bc27f8fbdbcead516fd8cbaee5ab8`, publication runtime artifact `726c8756-6024-49b3-a5c3-836fa2024d7f`

approval_id: `d08fdab8-0d3e-47fb-b584-f386a8a1ef71`

plan revision: `1`, 2/2 steps completed after approval; approval persisted as `APPROVED`

## Result

**PASS.** Approval state, checkpoint state, ledger continuity, and Worker restart recovery were real and durable. The final approval caused a real publication side effect on the integration test account.

## Problem

The public Java publication projection did not expose a resource ID in the final runtime projection, although the publish request was sent and the execution completed. This is an evidence/projection completeness issue, not a duplicate-execution issue.

## Fix

The approval and execution recovery paths were kept unchanged. The remaining improvement is to persist and project the Java publication resource ID consistently in the Agent runtime result and frontend history view.

