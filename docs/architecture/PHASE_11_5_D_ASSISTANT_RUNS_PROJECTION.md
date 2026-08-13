> **Historical document.** Retained for traceability; it is not the current architecture authority. See [CURRENT_ARCHITECTURE.md](CURRENT_ARCHITECTURE.md).

# Phase 11.5-D assistant_runs Projection Migration Audit

## 结论

`assistant_runs` 目前还不是纯粹的 Legacy projection。它仍被 API 主动写入，并且部分 Legacy API 仍直接从该表读取或更新状态。因此本阶段不删除、不修改 schema，也不宣布可以退休。

目标架构应为：

```text
Runtime canonical source:
PlanExecution + ExecutionStateManager + ExecutionEventStore

Legacy compatibility projection:
assistant_runs
```

`RunExecutionLink` 是两套 ID 的兼容映射，不是第二个状态源。

## 1. assistant_runs 写入路径

### 1.1 Repository

`packages/assistant_core/greenbook_assistant_core/db/repositories.py` 中的 `RunRepository` 对 `assistant_runs` 提供：

- `create()` -> `INSERT assistant_runs`
- `update()` -> 乐观锁 `UPDATE assistant_runs`
- `find_by_id()` -> 单条读取
- `find_all_by_user()` -> 用户历史列表

表当前包含 `status`、`content`、`error_code`、`error_message`、`tool_rounds`、`events`、`approval_id`、`session_snapshot` 和 `partial_results` 等 Legacy/API 快照字段；没有 `execution_id` 列。

### 1.2 API 主路径

`apps/assistant_api/greenbook_assistant_api/api/routes.py` 的 `send_message()`：

1. 创建 `run_id`。
2. 调用 `AssistantService.execute()`。
3. 通过 `RuntimeResult.execution_id` 建立 `RunExecutionLink`。
4. 无论 Runtime 还是 Legacy 结果，都调用 `_create_run()` 写入 `assistant_runs`。

因此 Runtime-backed request 目前仍会写入一份完整的 run 快照。该写入是兼容投影的候选入口，但尚未被明确限制为 projection writer。

### 1.3 其他主动更新

Legacy API 在以下路径直接更新 `assistant_runs`：

- `POST /runs/{run_id}/cancel`：Legacy-only 时写入 `status=CANCELLED` 和旧事件。
- `POST /runs/{run_id}/interrupt`：Legacy-only 时写入 `status=CANCELLED` 和旧事件。

Runtime-backed cancel/interrupt/resume 通过 `RunOperationAdapter` 调用 Execution Runtime，不应继续把执行状态写回 `assistant_runs`。主响应仍由 `get_run()` 组装，因此存在 projection 新鲜度问题。

`RuntimeAgentService` 与 `AssistantService` 不直接操作 `RunRepository`；它们返回 `RuntimeResult`，由 API 边界持久化 run 快照。

## 2. assistant_runs 读取路径分类

### A. 必须迁移到 execution_id / Runtime

这些读取涉及当前执行状态或执行控制，不能继续以 `assistant_runs` 为真相源：

| 能力 | 当前路径 | 目标 |
|---|---|---|
| 当前 status | `GET /runs/{run_id}` 读取 `record.status` | 通过 link 查询 `PlanExecution.status` |
| cancel | Legacy-only 直接更新 run；Runtime 已走 adapter | Runtime 始终调用 `ExecutionStateManager.cancel_execution()` |
| interrupt | Runtime 已走 operation adapter；Legacy-only 保留 | Runtime 语义统一到 execution control |
| resume | Runtime 已走 operation adapter；Legacy-only 保留旧查询行为 | Runtime 使用 `ExecutionStateManager.resume_execution()` |
| events | mapped run 使用 Execution EventStore，Legacy-only 使用 `record.events` | 保持该分流，禁止 Runtime 读取旧 events |
| SSE | mapped run 使用 execution stream，Legacy-only 使用 `record.events` | 保持 adapter 分流 |
| approval | Runtime approval 已用 `execution_id`；run 入口通过 adapter | approval service 使用 canonical Runtime approval |

`GET /runs/{run_id}` 目前仍主要依赖 run snapshot，并只通过 adapter 补充 `execution_reference`。这是下一阶段最明显的状态查询迁移点。

### B. 可以保留为历史查询或兼容展示

以下字段不属于 Execution 状态源，可以继续保留在 projection：

- `run_id`
- `conversation_id`
- `user_id`、`tenant_id`
- Legacy 请求的原始 `content` / `final_response`
- `trace_id`
- Legacy `tool_rounds` 汇总
- `created_at`
- `session_snapshot`、`partial_results`
- Legacy-only 的旧 `events`
- `approval_id` 作为兼容关联字段

这些字段的读取应服务于旧客户端、历史列表和审计展示；不能用于覆盖 Runtime 的当前状态。

### C. 可以删除的读取

当前没有可以立即删除的读取路径。原因是：

- `/runs`、`/runs/{run_id}` 仍是 Legacy API contract。
- Legacy-only run 仍没有 execution link。
- 前端和部分 Java/API 集成仍依赖 `run_id` 与旧响应结构。

未来只有在 Legacy-only 数据不再产生、客户端完成 execution reference 迁移、且历史查询替代方案稳定后，才可删除旧 `events` 状态查询和直接 status 读取。

## 3. Projection 字段设计

### 3.1 Runtime 来源字段

Runtime-backed projection 的以下字段应由 `PlanExecution` / `ExecutionEventStore` 派生：

- `status`
- `execution_id`，通过 `RunExecutionLink` 暴露，不要求修改 `assistant_runs` schema
- 当前 step / progress
- `updated_at`，以 Execution 更新时间为准
- 完成或失败状态
- Runtime step/event timeline
- retry 与 approval 状态

当前实现尚未完全做到这一点：`assistant_runs` 没有 `execution_id` 列，`get_run()` 的 status/content/steps 仍优先使用 run snapshot；只有 mapped events/control response 部分使用 Runtime 数据。

### 3.2 继续保留的 Legacy metadata

保留但不作为 Runtime 状态源：

- `run_id`
- conversation/user/tenant 归属
- 原始用户请求和兼容响应文本
- Legacy trace 与旧 tool 汇总
- Legacy-only `events`
- session 与 partial result 快照

### 3.3 推荐 Projection Writer

后续应建立明确的 API/application projection boundary，职责仅为：

1. 创建兼容 run 记录和 metadata。
2. 在 Runtime 返回 execution 后保存 `RunExecutionLink`。
3. 从 `PlanExecution` 和 EventStore 刷新 Runtime-backed 展示字段。
4. 不调用 Worker，不修改 Execution 状态，不复制全部 EventStore。

在该 writer 明确前，不应把散落在 `send_message()`、cancel/interrupt fallback 中的 `assistant_runs.update()` 视为最终架构。

## 4. Runtime / Legacy 边界

```text
New Runtime request
  -> PlanExecution
  -> ExecutionStateManager / EventStore
  -> RunExecutionLink(run_id, execution_id)
  -> assistant_runs compatibility projection

Legacy-only request
  -> assistant_runs
  -> Legacy API / old event snapshot
```

规则：

- Runtime 状态只能由 Execution Runtime 修改。
- `assistant_runs` 不得覆盖 Runtime status、step、retry 或 approval 状态。
- Runtime event 不双写到 `assistant_runs.events`。
- Legacy API 可以接受 `run_id`，但 mapped request 必须先解析 `execution_id`。
- Legacy-only run 保持旧行为，直到完成单独迁移。

## 5. 迁移阶段建议

### CURRENT

- 所有 send-message 结果都会写 `assistant_runs`。
- Runtime ID link 已独立持久化。
- mapped events/SSE/control 已优先使用 Execution Runtime。
- status 和部分 detail 仍从 run snapshot 读取。

### MIGRATION

1. 让 Runtime-backed `get_run()` 通过 link 查询 Execution，再组装状态和 steps。
2. 将 `assistant_runs` 写入收敛为 projection writer，并区分 Runtime 与 Legacy payload。
3. 明确 Runtime projection 的刷新时机和失败重试策略。
4. 保留 Legacy-only 的直接读写和旧 `events`。
5. 增加一致性测试：Execution 状态变化后，Legacy response 不得返回旧 Runtime status。

### TARGET

- `PlanExecution` 是唯一执行状态源。
- `ExecutionEventStore` 是唯一 Runtime 事件源。
- `assistant_runs` 只保存 Legacy metadata 与兼容展示快照。
- 所有 mapped API 操作通过 `execution_id` 或 adapter 进入 Runtime。
- 只有 Legacy-only run 继续维护旧状态字段和旧事件快照。

## 6. 本阶段范围与验证

本阶段仅完成静态审计和设计，没有：

- 删除 `assistant_runs` 或 `RunRepository`
- 修改数据库 schema 或 migration
- 修改 Worker、Planner、ToolRuntime
- 修改 ExecutionStateManager 核心逻辑
- 修改 Runtime 状态模型

验证范围：

- 全仓静态 `rg` 扫描 `assistant_runs`、`RunRepository`、`run_id`
- 既有 `tests/compat/runtime` 测试保持为后续迁移基线

