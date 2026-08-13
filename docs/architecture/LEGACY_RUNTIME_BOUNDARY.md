> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# GreenBook Legacy Runtime Boundary Audit

本报告是 Phase 7.7-A 的只读审计结果。它描述当前 Legacy、Compatibility 和 ACTIVE 之间的边界，为后续迁移提供证据；本阶段没有修改、删除或移动源码。

## 1. 当前正式边界

GreenBook 的正式执行链是：

```text
User
  -> IntentSpec
  -> IntentValidator
  -> PlanningContext
  -> Planner
  -> TaskPlan
  -> PlanExecution
  -> ExecutionStateManager
  -> ExecutionWorker
  -> ToolRuntime / Capability
```

当前 ACTIVE 入口和实现包括：

| 路径 | 角色 | 证据 |
| --- | --- | --- |
| `packages/assistant_core/greenbook_assistant_core/task/` | IntentSpec、理解和校验 | `task/intent_models.py`、`task/understanding.py`、`task/intent_validator.py` |
| `packages/assistant_core/greenbook_assistant_core/planning/` | PlanningContext、Planner、TaskPlan | planning package 及其单元测试 |
| `packages/assistant_core/greenbook_assistant_core/execution/` | PlanExecution、StateManager、Worker、Runtime Guard、Retry、Checkpoint、Event | `execution/models.py`、`execution/state_manager.py`、`execution/worker.py` |
| `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py` | 新 Runtime 的 Assistant API 服务入口 | `RuntimeAgentService.execute()`、`_execute_single()` |
| `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py` | 面向 `execution_id` 的 Runtime 查询和 SSE API | `/executions/{execution_id}` 及其子路径 |
| `packages/creator_client/` | Assistant 到 Creator 的 HTTP 客户端边界 | `greenbook_creator_client/client.py` |
| `services/greenbook_mcp/` | Capability 到 Java、社区和 Creator 工具的适配 | MCP server 和 workflows |

Legacy 代码仍可能被兼容 API、独立部署或回归测试使用，不能仅因不属于上述链路就直接删除。

## 2. `community-assistant-agent`

### 2.1 角色和入口

| 项目 | 结果 |
| --- | --- |
| 路径 | `community-assistant-agent/` |
| 包身份 | `community-assistant-agent/pyproject.toml` 中的 `greenbook-community-assistant-agent` |
| 服务入口 | `community-assistant-agent/app/main.py` |
| 持久化 | `community-assistant-agent/app/database.py`，包含 `assistant_runs` 及关联表 |
| 执行实现 | `community-assistant-agent/app/worker.py`、`app/turn_pipeline.py`、`app/task_manager.py` 等 |
| 迁移 | `community-assistant-agent/migrations/versions/` 中多次围绕 `assistant_runs`、`run_id`、重试和执行可靠性演进 |
| 测试 | `community-assistant-agent/tests/`，包含数据库、工具运行和迁移测试 |

该服务不在根目录 `pyproject.toml` 的 uv workspace members 中，但它有自己的 `pyproject.toml`、`uv.lock` 和测试环境，因此是一个独立历史 Agent 工程，而不是 Assistant Core 的普通子模块。

### 2.2 Import 和 API 引用

当前扫描没有发现 ACTIVE `assistant_core` 或 `RuntimeAgentService` 直接 import `community-assistant-agent` 的 Python 模块。两套代码主要通过 HTTP/API、共享协议、数据库和独立进程边界关联。

仍存在的旧 API 证据：

- `contracts/assistant-openapi.yaml` 定义 `/api/v1/assistant/runs`、`/runs/{run_id}`、事件 SSE、cancel 和 interrupt。
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` 仍实现 run 查询、事件、取消、中断和 `RunResponse`。
- `.env.example` 使用 `ASSISTANT_IDENTITY_AUDIENCE=greenbook-assistant-runtime`，与 Java 签发 audience 和 Python Runtime 校验配置一致。
- `community-assistant-agent/app/main.py`、`app/database.py` 和 migrations 仍直接使用 `assistant_runs`。

结论：`community-assistant-agent` 不是 ACTIVE Runtime 的直接 import 依赖，但仍是兼容 API/部署/数据边界的一部分。

### 2.3 Tests、Docker 和 CI

- `.github/workflows/verify.yml` 的 `assistant-agent` job 以 `community-assistant-agent` 为 working directory，执行 `uv sync --frozen` 和 `uv run pytest -q`。
- `community-assistant-agent/tests/` 直接验证其数据库、迁移和工具运行行为。
- 根 `docker-compose.yml` 主要启动基础设施，不包含该 Agent 的独立服务容器；开发脚本和环境配置仍保留 Assistant service 相关配置。
- `scripts/run_p0_e2e.py` 仍按 `community-assistant-agent` 目录寻找 Assistant 进程并检查 `/api/v1/assistant/runs/{run_id}`。

### 2.4 是否仍参与运行

判断为：`COMPATIBILITY / LEGACY ACTIVE DEPLOYMENT SURFACE`。

它至少仍参与 CI 和 P0 E2E；是否在当前生产流量中运行，不能从本地源码单独证明。必须进一步核对部署清单、运行环境和流量开关后，才能将其降为纯 ARCHIVE。

## 3. `LegacyAgentService`

### 3.1 当前入口

| 路径 | 证据和作用 |
| --- | --- |
| `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py` | `LegacyAgentService` 包装旧 `greenbook_assistant_core.agent`，保留旧 tool-calling 和 run 行为 |
| `apps/assistant_api/greenbook_assistant_api/main.py` | 应用启动时 import 并实例化 `LegacyAgentService`，再注入 `AssistantService` |
| `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py` | 构造函数接收 `legacy`，通过 `RuntimeRouter` 在 Legacy 和 Runtime 间选择，并保留 fallback |
| `apps/assistant_api/greenbook_assistant_api/api/routes.py` | 旧消息执行路径中再次构造 `LegacyAgentService` 以处理兼容请求 |
| `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py` | `off` 固定 Legacy，`on` 固定 Runtime，`dual` 按场景选择 |
| `packages/assistant_core/greenbook_assistant_core/agent.py` | 被 Legacy service 包装的历史 Agent 实现 |

### 3.2 与 `RuntimeAgentService` 的关系

`RuntimeAgentService` 位于 `apps/assistant_api/.../services/runtime_agent_service.py`，负责理解/分解、PlanningContext、Planner、TaskPlan 和新 Execution/ToolRuntime 路径。`LegacyAgentService` 是平行的旧执行实现，不是 RuntimeAgentService 的内部组件。

`AssistantService` 是当前两者的兼容编排层：

```text
Assistant API
  -> AssistantService
       -> RuntimeRouter
            -> RuntimeAgentService
            -> LegacyAgentService
```

只要 `ASSISTANT_RUNTIME_MODE=off`、`dual` 的 fallback，或旧 API contract 仍存在，LegacyAgentService 就不能删除。

分类：`LEGACY`，但当前仍是 `COMPATIBILITY` 运行入口；不是 DELETE CANDIDATE。

## 4. `RunRepository`、`assistant_runs` 和 `run_id`

### 4.1 `RunRepository`

实现位置：

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 的 `RunRepository`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` 中的 repository wiring 和 `_find_run()`

API 仍使用：

- `RunResponse.run_id`
- `GET /api/v1/assistant/runs`
- `GET /api/v1/assistant/runs/{run_id}`
- `GET /api/v1/assistant/runs/{run_id}/events`
- `GET /api/v1/assistant/runs/{run_id}/events/stream`
- `POST /api/v1/assistant/runs/{run_id}/cancel`
- `POST /api/v1/assistant/runs/{run_id}/interrupt`

因此，`RunRepository` 目前不是无引用 dead code，而是旧 API persistence adapter。

### 4.2 `assistant_runs` persistence

`assistant_runs` 同时出现在两个边界：

- Assistant API/数据库 repository 的旧 run 记录定义。
- `community-assistant-agent/app/database.py` 的主 ORM 表及其迁移。

`community-assistant-agent/migrations/versions/002_harness_controls.py`、`003_saga_capabilities.py`、`009_governed_runtime.py`、`010_adaptive_execution.py`、`011_goal_target_binding.py`、`012_intent_deltas.py` 和 `020_execution_reliability.py` 都证明该表承载 checkpoint、attempt、retry、tenant、goal 和 step 关联等历史运行数据。

不能在没有数据迁移计划的情况下删除或重命名该表。

### 4.3 approval 和事件依赖

- `apps/assistant_api/.../api/routes.py` 通过旧 run 记录关联 approval、conversation 和事件返回。
- `packages/assistant_core/.../db/repositories.py` 同时包含 `ApprovalRepository` 等旧 API persistence。
- `contracts/assistant-openapi.yaml` 将 run 事件和 approval 相关结果暴露为公开 API contract。
- 新 Runtime 使用 `PlanExecution`、`ExecutionStateManager`、`EventStore` 和 `execution_id`，但当前 API 兼容层仍会保留 `run_id`。

### 4.4 是否可以 adapter 化

可以，但只能在 API 边界进行分阶段 adapter 化：

```text
旧 run API / run_id
  -> ConversationRunAdapter
  -> PlanExecution / execution_id
```

adapter 至少需要处理：

1. 旧 run 状态到 `ExecutionStatus` 的映射。
2. 旧事件到 `ExecutionEvent` 的读取映射。
3. cancel/interrupt/approval 的语义映射。
4. 历史 `assistant_runs` 数据的只读访问和迁移策略。
5. API contract 版本兼容及回滚。

在这些条件完成前，分类为 `COMPATIBILITY`，不是 `ARCHIVE` 或 `DELETE CANDIDATE`。

## 5. Creator 边界

### 5.1 `packages/creator_client/`

路径：`packages/creator_client/greenbook_creator_client/client.py`。

职责：

- 通过 HTTP 调用 `/api/v1/creator/tasks`。
- 轮询 Creator task 状态。
- 获取 artifact。
- 处理 publication handoff、timeout、unavailable 和 idempotency header。

调用方证据：

- `apps/assistant_api/greenbook_assistant_api/main.py` 创建 `CreatorClient`。
- `services/greenbook_mcp/greenbook_mcp_server/server.py` 接收并使用 Creator client。
- `services/greenbook_mcp/.../workflows/create_draft.py` 和 `revise_draft.py` 通过 MCP workflow 使用 Creator 能力。
- `tests/integration/test_assistant_runtime_contracts.py` 覆盖 Creator task contract。

分类：`ACTIVE`。它是 Assistant Runtime 到 Creator 服务的正式客户端边界。

### 5.2 `creator-agent/`

这是根目录下的完整独立 Creator 工程，包含自身的 API、graph、worker、memory、evaluation、persistence 和 tests。

证据：

- `creator-agent/pyproject.toml` 和 `creator-agent/uv.lock`。
- `creator-agent/app/` 的 Creator runtime 实现。
- `creator-agent/tests/` 的 graph、dispatcher、harness 和 publication handoff 测试。
- `.github/workflows/verify.yml` 的 `creator-agent` job 执行其独立 lint/test。

分类：`ACTIVE DEPLOYMENT CANDIDATE / OWNER REVIEW`。CreatorClient 的 HTTP contract 明确需要 Creator 服务，但本地源码尚不能单独证明该目录就是唯一部署源。

### 5.3 `services/creator_agent/`

该目录在根 `pyproject.toml` 的 workspace members 和 `uv.lock` 中注册：

- `services/creator_agent/pyproject.toml`
- `services/creator_agent/greenbook_creator_agent/`

当前源码树显示它是一个 workspace package，包含 `api`、`domain`、`graph`、`persistence`、`worker` 包入口；它与根目录 `creator-agent/` 存在实现/部署来源重叠风险。

分类：`COMPATIBILITY / DEPLOYMENT REVIEW`，不能直接认定为重复 dead code。

### 5.4 Creator 迁移目标

目标边界应收敛为：

```text
IntentSpec
  -> Planner / TaskPlan
  -> Capability
  -> CapabilityExecutor / ToolRuntime
  -> MCP
  -> CreatorClient
  -> 唯一 Creator Agent HTTP service
```

迁移前必须确认：

1. `creator-agent/` 与 `services/creator_agent/` 的唯一 deployment owner。
2. Docker、CI、health check 和环境变量实际使用哪一套。
3. `/api/v1/creator/tasks`、artifact 和 publication handoff contract 是否一致。
4. 数据库 migration、Redis namespace 和生产数据是否可迁移。
5. 所有 integration/E2E/Creator tests 是否指向同一服务。

确认唯一 owner 并完成 contract/data 回归后，未被采用的实现才可成为 `ARCHIVE CANDIDATE`；在此之前不应删除。

## 6. 分类总表

### ACTIVE

- `packages/assistant_core/greenbook_assistant_core/task/`
- `packages/assistant_core/greenbook_assistant_core/planning/`
- `packages/assistant_core/greenbook_assistant_core/execution/`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`
- `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`
- `packages/creator_client/`
- `services/greenbook_mcp/`
- `PlanExecution` / `ExecutionStateManager` / `execution_id` execution state

### COMPATIBILITY

- `apps/assistant_api/greenbook_assistant_api/services/assistant_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_router.py`
- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py` 中的旧 run API
- `packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中的 `RunRepository`
- `assistant_runs`、`run_id`、旧 approval/event contract
- `services/creator_agent/`
- `community-assistant-agent/` 的独立 CI/API/数据库兼容面

### ARCHIVE CANDIDATE

- 完成部署 owner 核验后未采用的 Creator 实现：`creator-agent/` 或 `services/creator_agent/` 其中之一。
- LegacyAgentService 和旧 run API：仅在默认流量完全切换、fallback 关闭、数据迁移和 contract 版本下线后。
- `community-assistant-agent/`：仅在独立部署、CI 门禁、旧 API 和数据库迁移均完成退出后。

### DELETE CANDIDATE

本次审计没有发现满足删除条件的 Legacy Runtime 文件。所有候选至少存在 API、CI、数据库、部署或测试证据，不能凭目录名或缺少直接 import 删除。

## 7. 后续迁移前置条件

1. 确认 `ASSISTANT_RUNTIME_MODE` 在生产环境的实际值和流量比例。
2. 对 `run_id` 和 `execution_id` 建立明确的 API adapter 与数据映射。
3. 为旧 run API 制定版本下线和历史数据只读策略。
4. 完成 `community-assistant-agent` 的部署、数据库和 CI owner 确认。
5. 完成 Creator 两套服务的 contract、health、migration 和 deployment 对照。
6. 迁移后运行 unit、integration、contract、evaluation 和 e2e 全量回归，再决定 archive/delete。
