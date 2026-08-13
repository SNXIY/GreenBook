# Phase 8.3 Fast Certification

认证日期：2026-08-13

本阶段只处理 P0-1、P0-2、P0-3，未重构既有运行时边界，也未重新执行已经通过的完整案例集。

## P0-1 — Integration Identity / Personal Data

**Problem:** Java 直接查询能够返回当前用户文章，但 Agent 查询曾显示空结果。

**Root cause:** JWT、AuthContext、MCP 和 Java current-user resolution 均解析到同一身份（`sub/uid=1`，tenant=`zhiguang`）。真正的不一致发生在 Agent ToolRuntime：`InvocationResult` 只接受字典数据，丢弃了 MCP 返回的列表，因此成功的文章列表被投影成空对象。

**Fix:** 保留 `InvocationResult.data` 的多态值，并在 Agent API 的异步结果投影中保留列表数据；新增回归测试覆盖集合型 ToolResult。

**Real evidence:**

- conversation_id: `373d1c75-8a96-4a3e-9021-ede346bc2546`
- run_id: `7791b85d-1dfb-4a3e-9e2d-f3b1811be59f`
- Java Agent Facade records: `20`
- Agent `community.list_own_posts` records: `20`
- identity: `uid/sub=1`, tenant=`zhiguang`

**Result:** PASS. Agent 与 Java 在同一 integration identity 下返回一致数据。

## P0-2 — Evidence Provenance

**Problem:** Agent 需要明确区分个人历史数据、社区数据、Creator 研究和模型推断，避免把社区趋势说成个人表现。

**Fix:** 在现有 `ToolResult` / `InvocationResult` 链路增加 `DataProvenance`，并由真实 MCP 工具标记 `PERSONAL_DATA` 或 `COMMUNITY_DATA`；Agent Loop 的反思提示要求依据 provenance 表述来源，个人数据不足时先明确说明。

**Real evidence:**

- conversation_id: `8529e175-b1d2-4af7-9430-a987d0363e05`
- run_id: `3b9846da-762a-4424-93ee-877531fa6fa2`
- `community.list_own_posts`: `45`, provenance=`PERSONAL_DATA`
- `community.search_public_posts`: `33`, provenance=`COMMUNITY_DATA`
- `analytics.get_post_performance`: real `AUTHENTICATION_FAILED`; no fabricated personal metrics were used
- final response explicitly stated that personal performance data was unavailable and that the continuation used community Java trends (`COMMUNITY_DATA`)

**Result:** PASS. Real evidence was source-labeled and the personal/community boundary was preserved, including the personal-metrics failure path.

## P0-3 — Existing Case F Completion

**Result:** The existing pending execution was approved and resumed; no new Case F task was created.

**Real evidence:**

- conversation_id: `8efbfde4-38d0-4346-863b-9ce3402bf9cf`
- run_id: `49830bb1-a4f2-4ac6-82e6-18c04d0bf9d7`
- task_id: `5e33aff7-9565-4c81-90dd-704517b223dc`
- execution_id: `e622ded9-f653-4804-ab35-134f0a3eb232`
- approval_id: `485d3846-13fa-4c5c-a4d5-1d57d3f92bc5` (`APPROVED`)
- Creator strategy task: `739968de-af24-4fd4-a91f-2621b2ab3872`
- Creator draft task: `c9e8a02c-f03f-4158-9318-4d57f1f47791`
- Creator artifact: `art_cbd600f52457354ec9d63ea8631a2cea7c817165b459cebe5e1deac16946b36a`
- draft_id: `345958715888898048`
- schedule_id / Java resource_id: `346094276926640128`
- goal_id: N/A — not exposed by the current run projection

The execution resumed as `COMPLETED`. Existing draft and Creator lineage were retained; approval resumed the schedule step and added the schedule evidence without duplicating the Creator task.

## Regression

All final checks passed:

- root pytest: `592 passed, 1 skipped`
- Creator pytest: `64 passed`
- Java: `mvn test -q` passed
- Frontend lint/typecheck passed
- Frontend production build passed
- active runtime Ruff passed
- `uv lock --check` passed
- `docker compose config --quiet` passed
- `git diff --check` passed (only existing line-ending warnings)

Non-blocking warnings remain in test cache permissions and frontend browser-data freshness; they did not fail the checks.

## Certification

**CERTIFIED**

The three Phase 8.3 acceptance conditions are satisfied: personal-data identity is consistent, provenance prevents personal/community conflation, and the existing complex Case F completed through approval to a real Java schedule resource. No Phase 8.4 work was started.
