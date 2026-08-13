# Phase 8.2 Case C — Evidence-Aware Replanning After Tool Failure

## Input

`分析最近社区AI文章，总结大家讨论最多的三个方向，然后写一篇趋势分析文章。`

## Execution Trace

```text
community.search_public_posts (query: AI Agent)
  -> real Java dependency failure
  -> DEPENDENCY_UNAVAILABLE observation
  -> DynamicPlanner plan revision
  -> same canonical read tool with changed query/scope (AI, larger page size)
  -> analysis
  -> Creator strategy
  -> Creator article
```

The runtime did not blindly replay the failed request. A distinct equivalent safe read tool was not available in the candidate projection, so the planner selected a changed-argument read strategy and completed the task.

## Evidence

| Field | Value |
|---|---|
| conversation_id | `2cbb4055-7c3a-4e70-8c2d-86263ed4146d` |
| run_id | `684994f1-3eb4-4513-adcf-fa70ee555b63` |
| execution_id | `1370a26e-70df-4e44-ab4c-c82e65fe08e0` |
| failed step | `search_recent_articles:1` |
| failed tool / error kind | `community.search_public_posts` / `DEPENDENCY_UNAVAILABLE` |
| replacement step | `replan-search_recent_articles:1-23e8236f29` |
| replacement result | `SUCCESS`, 8 real posts |
| analysis artifact | `038af925-b0d3-4ace-b67c-7c70bb20deac` |
| final draft artifact | `4ce6a242-911b-4c7c-bb09-027e7ae513e7` |
| Java draft | `345922758535942144` |
| Creator task / artifact | `01032785-59e6-419f-9313-27352f32c04c` / `art_23d3d25fd26fa12851ecfef4b67f02769bf386633e2322376f7d6575e458031c` |
| search artifact | `7d0752d3-74bb-4a70-9ac7-2f39e0ca1505` |

## Result

**PASS — dynamic changed-argument replan.**

## Problem

Phase 8.1 exhausted safe retries and stopped before demonstrating continuation.

## Fix

The observation now carries failure kind, request evidence, and candidate read-only capabilities into `DynamicPlanner`. Replanning remains policy-gated: unknown or non-idempotent writes are not replayed blindly.

## Limitation

This run proves a safe changed-argument replan, not a distinct Tool B. The candidate catalog did not expose a semantically equivalent alternative read tool for this failure.

