# Phase 11.6 Runtime-Only Migration Plan

## 1. 目标与边界

最终 ACTIVE execution architecture：

```text
IntentSpec -> Planner -> TaskPlan -> PlanExecution
-> ExecutionStateManager -> Worker -> ToolRuntime
-> ExecutionEventStore
```

目标是让 Runtime 成为唯一执行状态和事件真相源，同时以受控方式退休 Legacy execution contract。

本报告是迁移计划，不执行删除、移动、数据库变更或 Runtime 修改。

重要边界：`run_id` 在 Creator、Java integration、trace、TaskIntent 和测试中也有独立语义。本计划只迁移 Assistant Legacy execution 的 `run_id`，不对所有同名字段做全局替换。

## 2. 全仓扫描结果

静态扫描范围：`apps/`、`packages/`、`services/`、`tests/`、`scripts/`、`docs/`、根配置、Docker 和 GitHub Actions。

| 标识 | 命中文件数 | 主要现状 |
|---|---:|---|
| `run_id` | 141 | Assistant API、前端、TaskIntent、Human/Trace、Java/Creator contract、测试 |
| `assistant_runs` | 26 | `RunRepository`、API snapshot、历史文档/查询 |
| `RunRepository` | 19 | DB repository、API wiring、相关测试/文档 |
| `LegacyAgentService` / `LegacyAgent` | 24 | Assistant fallback、legacy implementation、测试/文档 |
| `community-assistant-agent` | 40 | CI、启动脚本、E2E、身份 audience、集成文档 |
| `RunExecutionAdapter` | 19 | Runtime link、Legacy API control/event/approval、compat tests |

代表性生产路径：

- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`
- `packages/assistant_core/greenbook_assistant_core/compatibility/history/run_execution_link.py`
- `apps/frontend/src/services/assistantService.ts`
- `apps/frontend/src/components/assistant/AssistantPanel.tsx`
- `packages/java_client/greenbook_java_client/client.py`
- `services/greenbook_mcp/greenbook_mcp_server/context.py` 与工具实现

外部运行依赖：

- `.github/workflows/verify.yml` 仍以 `community-assistant-agent` 为工作目录。
- `scripts/verify-all.ps1`、`scripts/smoke-test.ps1`、`scripts/runtime-report.ps1`、`scripts/setup-dev.ps1` 和 `scripts/e2e-test.ps1` 仍引用旧 Agent 或 `run_id`。
- `scripts/run_p0_e2e.py` 同时启动/验证旧 Assistant、Creator 和 Runtime 相关服务。
- `apps/frontend` 旧 Assistant UI 仍以 `run_id` 调用查询、取消、中断、恢复和重试 API。

## 3. 分类

### 3.1 必须迁移

#### Assistant API execution contract

- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
  - 将 `/runs/{run_id}` 的 Runtime-backed 查询改为通过 link 读取 `execution_id` 对应的 PlanExecution/EventStore。
  - 保留 Legacy-only 分支。
  - 逐步让 cancel、interrupt、resume、approval、events 和 SSE 只通过 Runtime operation boundary。
- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
  - 仅保留明确的 Legacy fallback；Runtime 成功路径不能进入此服务。
- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
  - 最终关闭 `ENABLE_LEGACY_AGENT_FALLBACK` 默认路径，先保留可观测配置。

#### Client and API consumers

- `apps/frontend/src/services/assistantService.ts`
- `apps/frontend/src/components/assistant/AssistantPanel.tsx`
- `apps/frontend/src/components/comments/CommentSection.tsx`
- `apps/frontend/src/pages/TaskCenterPage.tsx`
- `packages/java_client/greenbook_java_client/client.py`

这些消费者应从 `run_id` contract 迁移到 `execution_id` / `ExecutionReference`，保留旧字段只用于兼容版本。

#### Runtime-related persistence

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中的 `RunRepository` 和 `_runs`。
- `assistant_runs` 的 Runtime-backed status/events/step snapshot 写入。

先把它们降级为 Legacy metadata/projection，再评估删除。不能在 projection writer、历史查询和 Legacy-only 行为尚未稳定前删除。

#### Scripts, CI and integration

- `.github/workflows/verify.yml`
- `scripts/verify-all.ps1`
- `scripts/smoke-test.ps1`
- `scripts/runtime-report.ps1`
- `scripts/e2e-test.ps1`
- `scripts/run_p0_e2e.py`

必须先迁移 CI、诊断和 E2E 到 ACTIVE Runtime，或明确这些脚本只验证 Legacy compatibility；否则删除旧 Agent 会直接破坏开发/发布流程。

### 3.2 可以删除，但须满足前置条件

这些是目标状态的删除候选，不是当前批准删除清单：

- `RunExecutionAdapter`：所有客户端、API、approval、SSE 和历史数据查询完成 execution reference 迁移后删除。
- `RunRepository`：Legacy-only run 不再创建，历史查询已切换到 Execution/归档查询，projection 已停止写入后删除。
- `assistant_runs`：必须先完成数据保留/归档策略、数据库 migration、历史 API 退役和回滚窗口，最后才可删除表及 migration。
- `LegacyAgentService`：Runtime fallback 关闭并经过生产观察窗口，且所有失败行为有明确 Runtime 错误 contract 后删除。
- `packages/assistant_core/greenbook_assistant_core/agent.py` 中旧执行实现：无生产 fallback、无测试/脚本/文档依赖后删除或归档。
- Legacy event snapshot：Runtime-backed run 不再读取 `assistant_runs.events`，Legacy-only 数据完成归档后删除读取逻辑。

### 3.3 需要保留历史或独立语义

- `community-assistant-agent/`：当前仍有 CI、启动脚本、E2E、身份配置和集成文档引用，必须先独立完成服务迁移或明确停用流程。
- `apps/creator-agent/` 内部的 `run_id` / `creator_runs`：这是 Creator 自己的运行域，不等同于 Assistant `assistant_runs`，不能因同名而删除。
- `packages/java_client` 的 `agent_run_id`：在 Java/Assistant capability 与 trace contract 完成版本迁移前保留。
- `services/greenbook_mcp` 的 `agent_run_id`：作为工具调用 trace/业务关联字段，需单独定义 execution reference 透传后再改名。
- `TaskIntent.run_id`、`Context.run_id`、Memory/Trace 字段：先通过 ExecutionReference 适配，不能在本阶段删除。
- `tests/compat/history/`：在 adapter 退休前保留，作为历史兼容回归基线。
- `docs/` 中明确记录 Legacy contract 的架构文档：在所有消费者迁移完成前保留。

## 4. 分阶段删除顺序

### Phase 11.6-A：Contract and Inventory Freeze

目标：冻结边界，不删除运行代码。

1. 为每个 API response 明确 `execution_id` / `ExecutionReference` 的 canonical contract。
2. 盘点所有 Runtime-backed run，确认每条记录都有 `RunExecutionLink`。
3. 标记 Legacy-only 数据、Runtime projection 和 Creator 独立 run。
4. 为 API、前端、Java client、CI、E2E 建立迁移完成清单。
5. 增加一致性检查：Runtime status/event 不得从 `assistant_runs` 读取。

退出条件：没有未分类的生产 `run_id` 使用点；没有 Runtime 请求缺少 execution link。

### Phase 11.6-B：Runtime API and Consumer Migration

目标：让所有新请求和 Runtime-backed 旧入口只消费 Execution Runtime。

1. 迁移 `/runs/{run_id}` 的 mapped 查询、steps、status 和 progress。
2. 迁移前端、Java client、SSE、approval、cancel、interrupt、resume 和 retry。
3. 将 `assistant_runs` 写入收敛为 metadata/projection，禁止写 Runtime events/status 作为真相。
4. 将 Legacy fallback 改为显式 opt-in，并观察失败率、fallback 次数和行为差异。
5. 更新 CI/E2E/脚本以优先验证 execution API。

退出条件：Runtime-backed 请求不再依赖 `assistant_runs` 执行状态；Legacy-only 仍可通过明确兼容入口工作。

### Phase 11.6-C：Legacy Runtime Retirement

目标：停止新 Legacy execution 的产生，并进入数据归档窗口。

1. 关闭 `ENABLE_LEGACY_AGENT_FALLBACK` 默认值，保留紧急回滚开关。
2. 禁止新请求创建 `assistant_runs` 作为执行记录；仅保留必要 metadata projection。
3. 将 `community-assistant-agent` 从 CI、Docker、启动脚本和 E2E 中移除前，完成独立服务停用确认。
4. 将 Legacy events、旧 Agent 响应和 Legacy-only runs 设为只读历史数据。
5. 运行生产观察窗口并验证 Runtime pause/resume/retry/approval/recovery。

退出条件：无生产流量进入 Legacy Agent；无新 Legacy-only run；历史数据已有归档和恢复方案。

### Phase 11.6-D：Adapter, Repository and Schema Retirement

目标：最后删除兼容基础设施。

1. 删除 API 的 run routes 和 `RunExecutionAdapter`，保留正式 execution routes。
2. 删除 `LegacyAgentService`、旧 `agent.py` 和 Legacy event readers。
3. 停止 `RunRepository` 后，迁移/归档历史 `assistant_runs`。
4. 在独立数据库 migration 中删除 `assistant_runs` 表及其不再使用的 migration，保留可回滚备份。
5. 删除仅用于兼容的测试、脚本和文档；保留 Runtime/evaluation 测试。

删除顺序必须是 API consumers -> fallback -> adapter -> repository/readers -> database schema，不能反过来。

## 5. 不应执行的全局替换

以下替换会混淆不同运行域，禁止作为迁移策略：

- 将所有 `run_id` 字段机械替换为 `execution_id`。
- 将 Creator `creator_runs` 当作 Assistant `assistant_runs`。
- 删除 `community-assistant-agent`，但保留 CI、脚本或 Docker 引用。
- 用 `assistant_runs` 反向同步或覆盖 `PlanExecution` 状态。
- 把 `RunExecutionAdapter` 改造成新的状态存储。

## 6. 当前风险与回滚

- API/前端仍存在旧 `run_id` contract，过早删除会破坏用户操作和 SSE。
- CI 和 E2E 仍显式依赖 `community-assistant-agent`。
- `assistant_runs` 仍承载 Legacy-only 数据和兼容响应，删除前需要历史读取方案。
- Creator 和 Java integration 使用相同命名但不同生命周期，误迁移会造成跨服务关联断裂。

每一阶段都应通过 feature flag、保留旧 API、数据库备份和可重新启用 fallback 回滚。Phase 11.6-D 之后不应再依赖这些回滚路径。

## 7. 本阶段结果

- 仅新增本迁移计划文档。
- 未删除代码、数据库表或 migration。
- 未移动文件。
- 未修改 Worker、Planner、ToolRuntime、ExecutionStateManager 或 Runtime 模型。
