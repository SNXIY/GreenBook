# GreenBook `run_id` -> `execution_id` Migration Design

本文件是 Phase 7.8-A 的迁移设计。当前阶段只记录边界、映射和迁移顺序，不修改 `RunRepository`、数据库、Worker、Execution Runtime 或 Planner。

## 1. 术语和原则

当前存在两个不同语义的 ID：

- `run_id`：旧 Assistant API 的一次 conversation turn/run 标识，也被旧 Agent、工具审计、部分上下文和测试使用。
- `execution_id`：`PlanExecution` 的执行实例标识，由 Execution Runtime 管理，关联 step、状态、checkpoint、retry、event 和 lease。

核心原则：

1. `PlanExecution` 是执行状态唯一真相源。
2. `RunRepository` 和 `assistant_runs` 在迁移完成前继续保留。
3. 迁移首先发生在 API/adapter 边界，不通过全局字符串替换改变业务语义。
4. `run_id` 可以作为外部兼容 ID，但不能继续作为新 Runtime 的状态主键。
5. `creator_run_id`、`agent_run_id` 等外部服务 ID 不自动等同于 `execution_id`。

## 2. CURRENT：`run_id` 使用位置

### 2.1 Assistant API 和响应模型

主要文件：

- `apps/assistant_api/greenbook_assistant_api/api/routes.py`
- `apps/assistant_api/greenbook_assistant_api/models/runtime_context.py`
- `apps/assistant_api/greenbook_assistant_api/models/runtime_result.py`
- `apps/assistant_api/greenbook_assistant_api/services/legacy_agent_service.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`

当前行为包括：

- `RuntimeContext.run_id` 由消息请求创建，并贯穿一次 Assistant turn。
- `RunResponse` 暴露 `run_id`、conversation、status、events URL 等字段。
- `routes.py` 创建 run record、写入事件、查询 run、返回 artifacts，并更新 `last_successful_run_id`。
- 旧消息执行入口根据 `run_id` 查询或创建 `RunRepository` 记录。
- `LegacyAgentService` 使用 `ctx.run_id` 作为旧 Agent 的执行关联 ID。
- `RuntimeAgentService` 已经创建 `PlanExecution.execution_id`，但仍将 `ctx.run_id` 用于 API result、trace 输出和工具调用上下文。

### 2.2 RunRepository 和 persistence

实现：

- `packages/assistant_core/greenbook_assistant_core/db/repositories.py`：`RunRepository`
- `apps/assistant_api/greenbook_assistant_api/api/routes.py`：repository 初始化、`_find_run()`、create/update/query 调用

`RunRepository` 当前提供：

- `create(**fields)`
- `find_by_id(run_id)`
- `find_all_by_user(...)`
- `update(run_id, **fields)`

它保存旧 API run record、status、events、tool rounds、conversation 和审批相关信息。它不是 `PlanExecution` 的别名，也不能在本阶段被替换为 `ExecutionRepository`。

### 2.3 `assistant_runs`

`assistant_runs` 的使用边界包括：

- `community-assistant-agent/app/database.py`：ORM 表定义和关联外键。
- `community-assistant-agent/app/main.py`、`app/worker.py`、`app/migrations.py`：旧 Agent 创建、领取和更新 run。
- `community-assistant-agent/migrations/versions/002_harness_controls.py`、`003_saga_capabilities.py`、`009_governed_runtime.py`、`010_adaptive_execution.py`、`011_goal_target_binding.py`、`012_intent_deltas.py`、`020_execution_reliability.py`：checkpoint、attempt、retry、tenant、goal、intent delta 和 step reliability 历史结构。

因此，`assistant_runs` 还承载历史数据和旧服务生命周期。迁移必须包含只读历史策略、双写或回填策略和回滚方案，不能只改 Python 类型。

### 2.4 SSE 和事件

旧 API contract 位于 `contracts/assistant-openapi.yaml`：

- `GET /api/v1/assistant/runs/{run_id}/events`
- `GET /api/v1/assistant/runs/{run_id}/events/stream`

实现位于 `apps/assistant_api/greenbook_assistant_api/api/routes.py`：

- `get_run_events(run_id, request)` 从旧 run record 读取事件。
- `stream_run_events(run_id, request)` 以旧 run ID 建立 SSE。
- 取消和中断会向旧事件列表写入 `RUN_CANCELLED` 或 `RUN_INTERRUPTED`。

新 Runtime SSE 位于 `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`，使用 `/executions/{execution_id}/stream` 和 `EventStore`，事件为 `ExecutionEvent`。

### 2.5 Approval、cancel 和 interrupt

旧路径：

- approval/pending 状态在旧 API、`ApprovalRepository`、conversation/run record 之间关联。
- `POST /api/v1/assistant/runs/{run_id}/cancel` 更新旧 run 为 `CANCELLED`。
- `POST /api/v1/assistant/runs/{run_id}/interrupt` 当前同样写入取消型旧状态/事件。
- `packages/security/greenbook_security/approval.py` 仍以 `run_id` 建立 approval request 关联。

新路径：

- `HumanInteractionRequest` 使用 `execution_id`。
- `ExecutionStateManager` 管理 `WAITING_APPROVAL`、`WAITING_HUMAN`、`PAUSED`、`CANCELLED` 等状态。
- `RuntimeManager` 和 `RuntimeGuard` 通过 `execution_id` 查询和控制 PlanExecution。

## 3. CURRENT：`execution_id` 使用位置

### 3.1 PlanExecution 和 StateManager

主要文件：

- `packages/assistant_core/greenbook_assistant_core/execution/models.py`
- `packages/assistant_core/greenbook_assistant_core/execution/repository.py`
- `packages/assistant_core/greenbook_assistant_core/execution/postgres_repository.py`
- `packages/assistant_core/greenbook_assistant_core/execution/state_manager.py`
- `packages/assistant_core/greenbook_assistant_core/execution/worker.py`

`PlanExecution.execution_id` 标识一次计划执行；其下关联 `StepExecution`、状态、current step、retry、checkpoint 和完成结果。所有 execution 状态变更应经由 `ExecutionStateManager`。

### 3.2 Runtime API 和事件

`apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py` 使用：

- `GET /executions/{execution_id}`
- `GET /executions/{execution_id}/steps`
- `GET /executions/{execution_id}/events`
- `GET /executions/{execution_id}/stream`

事件实现位于：

- `packages/assistant_core/greenbook_assistant_core/execution/events.py`
- `execution/event_store.py` 或 event stream adapter
- `ExecutionStateManager`、`ExecutionWorker`、`RuntimeManager`

Runtime 的 event payload、SSE 生命周期、checkpoint 和 recovery 都以 `execution_id` 为筛选键。

### 3.3 Worker、Artifact、Trace 和 Human Interaction

- `ExecutionWorker.run(execution_id)` 以 execution ID 读取并更新 PlanExecution。
- `RuntimeGuard.check_execution(execution_id)` 在 step 执行前阻止 paused/cancelled/waiting 状态继续执行。
- artifact 和 trace 模型使用 `execution_id` 关联执行事实。
- `HumanInteractionManager` 的 approval、clarification、resume 记录使用 `execution_id`。

这些使用属于 ACTIVE Runtime，不应为了兼容旧 API 改回 `run_id`。

## 4. MIGRATION：Adapter 设计

### 4.1 显式映射模型

建议新增 API 边界 adapter（本阶段只设计，不实现）：

```text
LegacyRunAdapter

resolve_run(run_id) -> RunExecutionLink
resolve_execution(execution_id) -> RunExecutionLink
to_legacy_response(link, execution) -> RunResponse
to_execution_status(link, legacy_record) -> ExecutionStatusView
```

逻辑映射记录应至少包含：

```text
RunExecutionLink
  run_id
  execution_id
  conversation_id
  task_id
  mapping_source       # CREATED, BACKFILLED, LEGACY_ONLY
  mapping_version
  created_at
```

这不是新的执行状态模型。它只是旧外部 ID 到 `PlanExecution` 的关联索引；状态仍由 `PlanExecution` / `ExecutionStateManager` 提供。

### 4.2 ID 生成和绑定时机

推荐绑定顺序：

```text
接受用户消息
  -> 生成兼容 run_id
  -> Understanding / Planning
  -> 创建 PlanExecution.execution_id
  -> 写入 run_id <-> execution_id link
  -> 返回旧 RunResponse + 新 execution_id
```

新 Runtime 创建执行时，应保证 link 写入与 execution 创建的失败行为可重试。不能使用 `run_id` 覆盖 `execution_id`，也不能通过字符串格式推断二者相等。

对于已经存在的旧 run：

- 如果能从 trace、task、checkpoint 或 run record 唯一找到 PlanExecution，则建立 `BACKFILLED` link。
- 如果只有旧 run 数据而没有 PlanExecution，标记为 `LEGACY_ONLY`，继续由旧 API 只读/兼容处理。
- 如果存在多个候选 execution，禁止自动猜测，进入迁移人工复核队列。

### 4.3 状态映射

状态映射必须是显式表，而不是直接复用字符串：

| Legacy run status | Target execution status | 处理说明 |
| --- | --- | --- |
| `QUEUED` / `PENDING` | `PENDING` 或 `CREATED` | 依赖是否已创建 PlanExecution |
| `IN_PROGRESS` / `RUNNING` | `RUNNING` | 仅当 execution 已取得有效状态和 lease |
| `WAITING_APPROVAL` | `WAITING_APPROVAL` | 保留 approval request 与 execution link |
| `WAITING_HUMAN` / `WAITING_INPUT` | `WAITING_HUMAN` | 不能误映射为普通 pause |
| `PAUSED` / `INTERRUPTED` | `PAUSED` | 用户暂停和旧 interrupt 语义需单独记录来源 |
| `SUCCEEDED` / `COMPLETED` | `COMPLETED` | 以 execution 完成事实为准 |
| `FAILED` | `FAILED` | 保留旧 error、error code 和 retry metadata |
| `CANCELLED` | `CANCELLED` | 只有明确取消语义才映射 |

如果旧 status 无法安全映射，应返回 `LEGACY_ONLY`，不能伪造 ExecutionStatus。

### 4.4 事件映射

旧事件记录通常是 `{event, data}`，新事件是 `ExecutionEvent`。建议只读 adapter 按以下原则映射：

| Legacy event | Execution event | 规则 |
| --- | --- | --- |
| `RUN_CREATED` | `EXECUTION_CREATED` | 只有 link 已建立时映射 |
| `RUN_STARTED` / `RUNNING` | `EXECUTION_STARTED` | 使用原始 timestamp |
| step start | `STEP_STARTED` | 必须有可确定的 step ID |
| step success | `STEP_COMPLETED` | 保留旧 payload |
| step failure | `STEP_FAILED` | 保留 error code/message |
| `RUN_CANCELLED` | `EXECUTION_CANCELLED` | 由用户取消映射 |
| `RUN_INTERRUPTED` | `EXECUTION_PAUSED` | 仅当旧 interrupt 的确表示用户暂停；否则保留 legacy event |
| approval required | `APPROVAL_REQUIRED` | 保留 approval ID 和旧 run ID |
| completed | `EXECUTION_COMPLETED` | 以 execution 状态校验结果 |
| failed | `EXECUTION_FAILED` | 以 execution 状态校验结果 |

映射后的事件应标记 `source=legacy`、原始 event 名称和 `run_id`，避免丢失审计信息。事件顺序和 timestamp 必须保持稳定；不能把旧事件重新当作新的状态变更写回 EventStore。

### 4.5 cancel 映射

推荐行为：

```text
POST /assistant/runs/{run_id}/cancel
  -> resolve run_id to execution_id
  -> ExecutionStateManager.cancel_execution(execution_id)
  -> return compatibility RunResponse
```

约束：

- 如果是 `LEGACY_ONLY`，继续使用旧 `RunRepository.update(..., status="CANCELLED")`。
- 如果已有 PlanExecution，不能只更新 RunRepository，否则会产生双状态。
- Worker 不需要被强行终止；后续 step 由 RuntimeGuard 阻止。
- cancel 产生的 target event 应由 Execution Runtime 产生，旧 API 只负责兼容响应。

### 4.6 approval 映射

approval 迁移建议：

```text
旧 approval(run_id)
  -> resolve execution_id
  -> HumanInteractionRequest(execution_id, type=APPROVAL)
  -> ExecutionStateManager.pause_for_approval / resume_execution
```

迁移要求：

- 保留 `run_id` 作为旧 approval API 的查询键，直到 contract 下线。
- 新 approval 的状态真相必须是 Human Interaction + PlanExecution，而不是旧 run record。
- approval accepted/rejected 必须幂等，避免旧 API 重试造成重复 resume。
- 没有唯一 execution link 的旧 approval 保持 `LEGACY_ONLY`，不能自动迁移。

### 4.7 历史数据策略

分三类处理：

1. **已关联历史数据**：回填 run/execution link，旧记录只读保留，新的状态查询走 PlanExecution。
2. **只有旧 run 的历史数据**：继续由 RunRepository 提供只读查询和旧 SSE，不强制生成虚假的 PlanExecution。
3. **无法关联或冲突数据**：保留原始数据，进入迁移异常报告，不自动删除或覆盖。

历史迁移至少需要：批量回填脚本设计、校验计数、冲突报告、审计日志、回滚/重跑策略和数据保留期限。数据库 schema 变更不属于本阶段。

## 5. TARGET：最终目标架构

### 5.1 新 API 为主

```text
User
  -> Assistant API
  -> IntentSpec
  -> Planner
  -> PlanExecution(execution_id)
  -> ExecutionStateManager
  -> Worker / ToolRuntime
```

用户可见的正式 Runtime API 以 `execution_id` 为主：

- execution status
- steps
- events
- SSE stream
- pause/resume/cancel
- approval/recovery

### 5.2 Legacy API 作为有期限 adapter

```text
旧 /assistant/runs/{run_id} API
  -> LegacyRunAdapter
  -> execution_id
  -> PlanExecution / EventStore / Human Interaction
  -> compatibility RunResponse / legacy event shape
```

最终 `RunRepository` 只可在以下条件全部满足后下线：

- 旧 API 已版本下线并完成客户端迁移。
- `run_id` 到 `execution_id` link 覆盖所有可迁移历史数据。
- approval、cancel、interrupt 和 SSE 已由新 API 覆盖。
- `community-assistant-agent` 的旧 API/数据库 owner 已确认退出。
- contract、integration、e2e 和数据回归全部通过。
- 历史 `assistant_runs` 已只读归档并满足保留策略。

### 5.3 ID 命名边界

最终建议：

- `execution_id`：GreenBook execution state、step、event、checkpoint、lease 和 Runtime API 的唯一执行键。
- `run_id`：仅保留为旧 API/versioned compatibility ID，或在明确的 conversation-turn 语义中使用。
- `agent_run_id`：外部工具/MCP audit 字段，在 contract 允许时携带 execution link，但不替代 execution_id。
- `creator_run_id`：Creator 服务内部 ID，不能与 GreenBook execution_id 混用。

## 6. 迁移阶段

### CURRENT

- 两套 ID 同时存在。
- 新 Runtime 已使用 `execution_id`，旧 API 仍使用 `run_id`。
- `RuntimeAgentService` 在同一请求中同时携带 `ctx.run_id` 和 `execution.execution_id`。
- `RunRepository`、`assistant_runs`、旧 SSE、approval、cancel 和 interrupt 仍可访问。

### MIGRATION

1. 增加只读 `run_id <-> execution_id` link adapter，不改变执行模型。
2. 新建 PlanExecution 时建立 link，并在兼容响应中同时返回两个 ID。
3. 新 API 和新服务内部统一使用 `execution_id`。
4. 旧 run 查询、事件、approval、cancel 和 interrupt 先经 adapter 路由。
5. 回填可唯一关联的历史数据，冲突数据进入报告。
6. 观察旧 API 流量和 Legacy fallback，再安排版本下线。

### TARGET

- PlanExecution/ExecutionStateManager 是唯一 execution state source of truth。
- 新 API、SSE、approval、cancel、retry 和 recovery 全部以 `execution_id` 为键。
- `run_id` 只存在于版本化兼容 adapter、历史只读数据和明确的 conversation-turn 语义。
- `RunRepository` 和 `assistant_runs` 在完成数据保留/归档后再评估下线。
- Worker、Planner、IntentUnderstanding 和 ToolRuntime 不承担 ID 迁移逻辑。

## 7. 风险和验收条件

主要风险：

- 双写状态不一致。
- 旧 SSE 与新 EventStore 事件重复或乱序。
- approval resume 使用错误 ID 导致任务无法恢复。
- `run_id`、`agent_run_id`、`creator_run_id` 被错误合并。
- 历史数据无法唯一映射到 PlanExecution。

验收必须覆盖：

- 新旧 API 对同一执行返回一致状态。
- cancel、pause/resume、approval accept/reject 幂等。
- 旧 SSE 和新 SSE 的事件顺序及终止语义稳定。
- 历史只读数据可查询且没有状态伪造。
- Runtime、Worker、Planner、ToolRuntime 现有测试不因 adapter 改变语义。

