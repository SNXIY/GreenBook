# Phase 11-A Durable Runtime Audit

## 目标与范围

本审计确认 Runtime 当前哪些事实可以跨进程恢复，哪些仍依赖单个 Python
进程。范围包括：

- `ExecutionRepository`
- `ExecutionEventStore`
- `CheckpointStore`
- `ExternalOperationStore`
- `RetryTaskStore`

本阶段不修改 Intent、Planner、Creator 或 Java 业务逻辑，也不删除 Legacy
代码。结论用于 Phase 11-A 至 Phase 11-D 的增量实现。

## 当前存储矩阵

| 数据 | 当前默认实现 | PostgreSQL/SQLAlchemy adapter | 重启后是否可恢复 |
|---|---|---|---|
| `PlanExecution` / `StepExecution` | `ExecutionRepository`，进程级内存 | `PostgresExecutionRepository`，复用 `execution` 与 `execution_step` 表 | 默认路径否；显式注入 PostgreSQL adapter 时是 |
| canonical execution events | `ExecutionEventStore`，进程级内存 | `PostgresExecutionEventStore`，复用 `execution_event` 表 | 默认路径否；显式注入时是 |
| checkpoint | `RuntimeManager._checkpoints`，进程级内存 | `PostgresCheckpointStore`，复用 `checkpoint` 表 | 默认路径否；显式注入时是 |
| external operation | `ExternalOperationStore`，进程级内存 | 尚无 | 否 |
| retry task | 尚无独立 `RetryTaskStore`；`RetryScheduler` 的 `_pending` 与 `_known` 为内存字典 | 尚无 | 否 |
| worker lease | `ExecutionLeaseManager`，进程级内存 | `PostgresExecutionLeaseManager`，复用 `execution_lease` 表 | 默认路径否；显式注入时是 |

已有 PostgreSQL adapter 使用同步 SQLAlchemy `Engine`/`Connection`，并以
`execution_metadata.create_all()` 创建表。现有测试用 SQLite 验证同一 adapter
契约，因此 operation store 也应保持同样的 bind 抽象，不把 PostgreSQL 驱动
细节泄漏到 Runtime 模型。

## 默认启动路径的事实

虽然 execution、event、checkpoint 和 lease 的 PostgreSQL adapter 已存在，
当前默认接线仍创建内存对象：

1. `RuntimeAgentService` 创建 `ExecutionWorker` 时没有注入持久化 repository、
   event store、checkpoint store 或 operation store。
2. Assistant API 的 runtime fallback 在 `main.py` / runtime routes 中使用
   `ExecutionRepository` 与 `ExecutionEventStore`。
3. `ExecutionWorker` 默认创建自己的 `ExecutionStateManager`、
   `RuntimeManager` 和 `ExternalOperationTracker`；未注入的 tracker 拥有
   内存 `ExternalOperationStore`。
4. `RetryScheduler` 没有数据库表、claim 方法或后台循环；进程退出后所有
   pending task 消失。

因此“已有 adapter”不等于“当前 Runtime 默认可恢复”。本 Sprint 只增加
可注入的持久化能力；生产启动路径接线应在明确数据库配置和生命周期边界后
单独完成。

## 失败与恢复时的丢失点

`ExecutionEvidence` 已通过 `InvocationResult`、`ExecutionResult`、
`ExternalAgentFailure` 和 `STEP_FAILED` event 传递，但 operation record
没有 canonical SQL 存储。进程重启后：

- `operation_id`、`external_operation_id`、receipt 和 status 不能从
  `ExternalOperationStore` 恢复；
- `RetryScheduler` 无法知道哪些 retry task 已创建、已 claim 或已消费；
- RecoveryService 可以从持久化 execution/event 恢复 step，却不能恢复
  external operation 查询上下文或 pending retry task。

这会造成两类风险：重复排队同一 attempt，或因本地记录消失而把 UNKNOWN
结果错误地当作没有历史。

## Phase 11 实施边界

### Phase 11-A

增加 `external_operation` 表和 `PostgresExternalOperationStore`，保存完整
`ExternalOperationRecord`（包括 evidence JSON、幂等键、receipt 和更新时间）。
内存 `ExternalOperationStore` 保持兼容；以 `operation_id` 为幂等主键，并提供
`execution_id`、`external_operation_id` 和 receipt 查询。

### Phase 11-B

增加独立 `RetryTaskStore` 抽象及可选 PostgreSQL 实现，后台 worker 扫描
READY task，检查 deadline/budget，再调用 `RetryManager`。claim 必须是
幂等的，shutdown 必须停止扫描且不丢失未 claim task。

### Phase 11-C

让 `ReconciliationService` 输出 `ReconciliationResult`，由显式 recovery
integration 根据：

- `SUCCEEDED`：恢复对应 execution step 的完成事实；
- `FAILED`：记录明确失败；
- `NOT_FOUND`：保留人工处理所需的 UNKNOWN/人工路径；
- `UNKNOWN`：保持等待/未知事实。

该 integration 不重放工具、不做补偿，也不新增 `WAITING_DEPENDENCY` 或
`UNKNOWN_RESULT` Execution 状态。

### Phase 11-D

增加 `ExternalOperationAdapter` 查询接口和 mock adapter。Creator/Java 的
真实 HTTP 查询实现只在后续接线阶段加入；本阶段不修改它们的业务代码。

## 结论

当前 Runtime 已经具备 execution/event/checkpoint 的 PostgreSQL adapter，
但默认启动仍是内存路径；ExternalOperation 和 RetryTask 完全没有持久化。
Phase 11 的最小可靠闭环是：

```text
PostgreSQL operation/task records
        ↓
restart-safe background worker
        ↓
RetryManager / ReconciliationService
        ↓
explicit Execution recovery decision
```

在此闭环完成前，不应声称 Runtime 已经具备跨进程的 Retry 或 UNKNOWN 结果
恢复能力。
