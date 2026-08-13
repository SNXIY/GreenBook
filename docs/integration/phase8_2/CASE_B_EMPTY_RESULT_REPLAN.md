# Phase 8.2 Case B — Empty Result Adaptive Planning

## Input

`分析最近社区Java Agent内容，找到值得做的专题方向，写第一篇内容。`

## Execution Trace

```text
Command
  -> Goal / Task plan
  -> community.search_public_posts
  -> EMPTY observation (resource_count=0)
  -> evidence-bounded planner decision
  -> ASK_HUMAN
  -> no Creator call and no fabricated post reference
```

## Evidence

| Field | Value |
|---|---|
| conversation_id | `1feded68-639c-4d18-b904-12f37e28a0d2` |
| run_id | `67ffff21-7b73-44a7-93fe-fbf3866a99e2` |
| execution_id | `N/A — planner stopped before creating an execution` |
| task_id | `N/A — no task was created` |
| goal_id | `N/A — no durable goal was created` |
| artifact_id | `N/A — no artifact was created` |

The real search response was empty. The runtime preserved `result_status=EMPTY`, `resource_count=0`, and the missing-evidence condition. It did not invent a `post_id`, call `community.get_post`, or claim completion.

## Result

**PASS — safe evidence-bounded stop.**

## Problem

Before the Phase 8.2 change, an LLM `CONTINUE` decision after an empty read could pass through without a second evidence check.

## Fix

`DynamicPlanner` now repairs an empty-result decision through the same planner boundary. A validated read-only alternative or changed read arguments may continue; otherwise the result is `ASK_HUMAN`. The fallback is selected from the available tool projection, not from a fixed tool alias.

## Remaining Evidence

This case proves safe empty-result handling. Case F also records a separate remaining gap: after an empty alternative read, a larger mixed-data task still continued into Creator generation without a user-facing provenance block.

