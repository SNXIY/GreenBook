# Phase 8.1 Case 3 — Tool Failure and Dynamic Adjustment

## Input

`帮我分析最近社区AI文章，总结大家讨论最多的三个方向，然后写一篇趋势分析文章。`

## Execution Trace

Baseline run:

```text
Frontend -> Agent API -> Command -> GoalTree -> Plan
-> Worker -> MCP community.search_public_posts
-> content-pattern analysis -> Creator Service
-> Creator artifact -> Java draft
```

Baseline real execution completed three steps: search, analysis, and Creator generation.

Induced-failure run:

```text
Command -> GoalTree -> Task
-> community.list_own_posts
-> Java backend unavailable
-> bounded safe retries (3)
-> observation / failure decision
-> WAITING_HUMAN fallback decision
```

The normal Java process stayed online for the rest of the system; the temporary real Worker configuration pointed its Java client at an unavailable endpoint. No write operation was sent.

## Evidence

conversation_id: `fdec2328-6037-40b4-8043-d0ac94eb6ebd` (induced failure); baseline `cbeb943a-6bf3-4b20-95ee-3256b809eb93`

run_id: `044b9ced-78d3-4aad-acf7-abf93c07a94a` (induced failure); baseline `ce9ce273-518a-4096-9ab6-c8cf836e6ad0`

execution_id: `N/A` for the induced branch because it stopped before a durable Execution was created; baseline `16e60b05-5e43-4d7b-9cec-78fa777543b7`

task_id: `c7e71abf-3d55-489c-9b42-4123de6c23a6` (induced); baseline `6dd047e6-8f82-4901-8b51-67a98fa766eb`

goal_id: `g_analyze_recent_ai_articles`, `g_list_recent_ai_articles`, `g_summarize_top_three_directions`, `g_write_trend_analysis_article`

artifact_id: baseline search `341ba0d8-b3d6-45d0-8a7a-2215e3146959`, analysis `66657a3c-3e1c-437d-9353-6801aa679a0f`, draft `6fe69a6e-2bb9-4a6d-ad02-5c6394e7fe58`; induced branch `N/A`

plan revision: baseline `1`; induced branch had no Plan Revision after failure

## Result

**PARTIAL.** The baseline passed through the real Worker, MCP, Creator, and Java draft handoff. The induced failure proved bounded retry, no-write safety, and human fallback. It did not prove automatic alternative-tool selection or a persisted Plan Revision.

## Problem

After the safe retry budget was exhausted, the runtime asked for human direction: wait for Java recovery, use `community.search_public_posts` as a fallback, or reuse an earlier result. It did not autonomously choose the valid alternative and replan.

## Fix

No hard-coded workflow was added. The failure remains an explicit Phase 8.1 gap: add a policy-driven alternative-tool/replanning decision after observation, with capability and data-availability checks, then rerun this case under the same induced fault.

