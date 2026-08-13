# Phase 8.1 Case 5 — Multi-agent Collaboration

## Input

`我要运营一个Java Agent专题栏目。帮我分析当前社区用户兴趣，设计栏目规划，生成第一期内容，准备推广方案。`

## Execution Trace

```text
Frontend -> Agent API -> Command -> GoalTree
-> TaskManager -> multi-goal Plan / dependency graph
-> Worker -> Tool Runtime / MCP
-> community.search_public_posts (real Java backend)
-> target-specific community.get_post validation
-> fail-closed before downstream Creator handoff
```

The intended graph was `analyze_community_interests -> design_column_plan -> generate_first_issue_draft -> prepare_promotion_plan`, with Creator work only after the analysis dependency. A post-compiler run materialized the complete dependency structure instead of truncating the plan to the first two goals. A later resource-reference run reached real Java search, received an empty result set, and correctly refused to invent a `post_id`.

## Evidence

conversation_id: `2c484092-f042-4ac0-b254-159a7b7572be`; earlier pre-fix run `bb593644-9116-4dc1-a1b6-0805ee6ca98c`

run_id: `eecf8485-c38f-4830-a675-f37742a61e17`; earlier post-compiler run `54cf3c95-3401-4601-9219-eec9e204cf03`

execution_id: `f4175c24-4aac-4747-b425-e7724dfde51d`; earlier post-compiler execution `1162f10d-34bb-48e5-9517-642503be3b05`

task_id: `4ef97eb3-466d-4eb6-bc26-d80a386891be`; earlier pre-fix Task `b5c4f413-7691-40c1-a6a4-38b31c282fc3`

goal_id: `operate_java_agent_column`, `analyze_community_interests`, `design_column_plan`, `generate_first_issue_draft`, `prepare_promotion_plan`

artifact_id: search result `6d3d4625-39f3-4739-beec-b3a4ea380111`; earlier search result `1a7f4782-8a9c-4629-9093-e93f9d88cb6f`; no Creator artifact or draft was created in the latest failed run

plan revision: latest task projection `goal_tree_version=3`, `plan_version=5`; post-compiler run materialized a 15-step plan, while the latest run stopped at `GET_POST_DETAIL`

## Result

**FAIL for end-to-end completion; PARTIAL for architecture validation.** The real multi-goal path and failure-safe dependency handling were exercised, but the case did not reach Creator, artifact generation, promotion planning, or final completion.

## Problem

The LLM selected target-specific `community.get_post` after a real search returned no posts. No `post_id` was available, so schema validation rejected the call before an external write. The runtime then skipped dependent goals instead of selecting a general trend-analysis fallback.

## Fix

Implemented and unit-tested: completion of partial LLM TaskNode hints into executable capabilities, persisted tool metadata across queue boundaries, stable resource-reference propagation, and prompt constraints against target-specific tools without a target. The remaining fix is a runtime replanner that can lower the analysis scope or choose `community.search_public_posts`/aggregate analysis when the result set is empty, without fabricating data.

