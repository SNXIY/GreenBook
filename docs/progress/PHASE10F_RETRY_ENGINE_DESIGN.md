# Phase 10-F：Evidence-aware Retry Engine 设计

## 0. 范围与设计结论

Phase 10-F 建立统一的 Retry 决策边界，目标是把旧的：

```text
StepExecution.error_code
    ↓
RecoveryPolicy
    ↓
FAILED_RETRYABLE → PENDING
```

演进为：

```text
Failure
    ↓
FailureClassifier / FailurePolicy
    ↓
ExecutionEvidence 安全检查
    ↓
RetryDecision
    ↓
Worker / Retry API / resume / RecoveryService
```

本设计不针对任何单一错误码，不改变 Planner、IntentSpecProvider、TaskProvider、
TaskOrchestrator、Creator Agent、Java Backend 或前端，也不新增
`WAITING_DEPENDENCY`、`UNKNOWN_RESULT` 等 Execution 状态。

核心结论：

> RetryDecision 是唯一的 Retry 授权事实。旧的 `retryable`、
> `FailureClassification.retryable` 和 `RecoveryPolicy` 只能作为输入或兼容信息，
> 不能直接把 Step 重置为 `PENDING`。

---

## 1. 当前问题与边界

Phase 10-E 已经让一次调用可以携带：

- `request_sent: False | True | None`；
- `side_effect_state`；
- invocation/tool/operation identity；
- receipt 和 external operation reference（上游提供时）；
- runtime/external idempotency key；
- response、异常类型、phase 和 trace。

当前缺口在于这些事实在 Worker 失败决策之后没有进入 RetryManager、resume 和
RecoveryService 的消费边界。因此，本阶段需要：

1. 在 `STEP_FAILED` event 中保存可序列化的 Evidence snapshot；
2. 由统一 resolver 从当前结果或 event store 恢复 Evidence；
3. 由同一个 RetryDecisionEngine 对所有 Retry 入口执行 fail-closed 检查；
4. 仅在 Decision 允许时使用现有 `FAILED_RETRYABLE → PENDING` 转换；
5. 将需要对账的外部操作交给后续 Phase 10-G/H，不在本阶段盲目重放。

`ExecutionStateManager` 的持久化 schema 不承载 Evidence；event payload 是本阶段
的证据快照边界。使用持久化 EventStore 时，快照可以跨 Runtime 进程恢复；没有
持久化 EventStore 时，系统只能对当前进程内事实作决定，缺失证据时必须拒绝自动
Retry。

---

## 2. RetryDecision 模型

### 2.1 输出模型

建议新增 `RetryDecision`，位于 Runtime execution package：

```python
class RetryDecision(BaseModel):
    allowed: bool
    reason: str
    retry_after: datetime | None
    max_attempts: int
    backoff: float
    requires_reconciliation: bool
    requires_user_confirmation: bool
    evidence_requirements: tuple[str, ...]

    category: FailureCategory
    raw_error_code: str
    attempt: int
    retry_budget: int
    operation_id: str | None
```

字段职责：

| 字段 | 语义 |
|---|---|
| `allowed` | 当前事实和策略下是否允许把这次失败作为 Retry 候选 |
| `reason` | 可审计的拒绝或允许原因，不改变原始失败事实 |
| `retry_after` | 最早可调度时间；立即 Retry 时为空或等于当前时间 |
| `max_attempts` | 该 Step/策略允许的最大尝试次数 |
| `backoff` | 本次等待秒数；不承担 sleep 或调度副作用 |
| `requires_reconciliation` | 是否必须先查询外部 operation/receipt |
| `requires_user_confirmation` | 是否需要用户明确确认后才可继续 |
| `evidence_requirements` | 缺失或必须核验的证据字段 |
| `category` | 来自 FailureClassifier 的稳定类别 |
| `raw_error_code` | 保留下游原始错误码 |
| `attempt` / `retry_budget` | 本次决策的次数上下文 |
| `operation_id` | 供后续 ExternalOperationTracking 关联，不能用来代替事实 |

`RetryDecision` 是不可变数据，不调用外部服务、不 sleep、不修改 Execution 状态。

### 2.2 输入上下文

`RetryDecisionEngine` 接收：

```text
ExternalAgentFailure
FailureClassification / RecoveryDecision
ExecutionEvidence
Step attempt / retry budget / max attempts
ToolContract retry and side-effect metadata（如果当前调用方可提供）
execution deadline
user_requested_retry
```

如果 `ExecutionEvidence` 缺失，或者 Evidence 中的关键字段无法解析，Engine 必须
返回 `allowed=False`，并填写 `evidence_requirements`；不能用
`ExecutionResult.request_sent` 的 legacy 默认值 `False` 代替未知事实。

---

## 3. Evidence 安全矩阵

### 3.1 自动 Retry 允许条件

自动 Retry 至少同时满足：

1. FailureCategory 属于暂时性类别：`DEPENDENCY_UNAVAILABLE`、`TIMEOUT`、
   `NETWORK_ERROR` 或受控的 `RATE_LIMIT`；
2. `request_sent=False`；
3. `side_effect_state=NONE` 或 `NOT_STARTED`；
4. retry budget 和 `max_attempts` 未耗尽；
5. execution deadline 允许下一次调用；
6. ToolContract 没有禁止重放；
7. 没有用户输入、认证、权限、契约或业务修正前置条件。

`request_sent=False` 本身不能覆盖冲突的 side-effect 事实。如果
`side_effect_state=POSSIBLE/UNKNOWN`，以风险更高的一侧为准。

### 3.2 矩阵

| `request_sent` | `side_effect_state` | 自动 Retry | Decision |
|---|---|---:|---|
| `False` | `NONE` | 允许候选 | 仍检查类别、预算、deadline 和 ToolContract |
| `False` | `NOT_STARTED` | 允许候选 | 同上 |
| `False` | `POSSIBLE` | 禁止 | `requires_reconciliation=True` |
| `False` | `UNKNOWN` | 禁止 | `requires_reconciliation=True` |
| `True` | `NONE` / `NOT_STARTED` | 默认禁止 | 必须有权威 ledger 证明且单独评估 |
| `True` | `POSSIBLE` | 禁止 | 进入 reconciliation |
| `True` | `UNKNOWN` | 禁止 | 进入 reconciliation |
| `True` | `CONFIRMED` | 禁止同一 operation 重放 | 使用已有结果或后续动作 |
| `None` | `NONE` / `NOT_STARTED` | 默认禁止 | 除非未来有可验证的权威证据来源 |
| `None` | `POSSIBLE` / `UNKNOWN` | 禁止 | 进入 reconciliation 或人工路径 |
| `None` | `CONFIRMED` | 禁止同一 operation 重放 | 使用确认结果 |

本阶段实现采用更保守的最小规则：只有明确的
`False + NONE/NOT_STARTED` 才能得到 `allowed=True`。其他组合不通过自动 Retry
授权。

---

## 4. 四类 Retry 入口统一设计

### 4.1 Worker 失败路径

```text
ExecutionResult
    ↓
FailureNormalizer / ExternalAgentFailure
    ↓
FailureDecisionEngine
    ↓
RetryDecisionEngine
    ↓
STEP_FAILED + evidence + decision snapshot
    ├─ allowed=True  → FAILED_RETRYABLE（等待后续执行）
    ├─ reconciliation_required=True → FAILED + ExternalOperationRecord
    └─ otherwise → FAILED / REQUEST_USER_INPUT
```

Worker 不在当前 pass 内执行第二次 Tool。它只保存决策和事实，并继续使用现有
Execution 状态模型。

### 4.2 Retry API

```text
POST /executions/{execution_id}/steps/{step_id}/retry
    ↓
读取最近 STEP_FAILED 的 Evidence
    ↓
同一个 RetryDecisionEngine(user_requested_retry=True)
    ├─ allowed=True  → 立即或延迟排队
    └─ allowed=False → 保持失败，返回拒绝原因
```

用户请求不能覆盖 `UNKNOWN`、`POSSIBLE` 或缺失证据的安全门。它只能作为策略上下文
记录，不能把未知结果强制变成可重放。

### 4.3 Resume

用户 Resume 首先区分生命周期恢复和 Retry：

- 普通 `PAUSED → RUNNING` 不应被误判为 Retry；
- 如果 Resume 会重置 `FAILED_RETRYABLE`，每个待重置 Step 先通过同一个
  `RetryDecisionEngine`；
- Decision 拒绝的 Step 不能被旧的批量 resume 逻辑直接清空错误并重放。

本阶段不增加新的等待状态；被拒绝的 Step 保持失败或由上层展示对账/人工原因。

### 4.4 RecoveryService

进程恢复读取持久化 EventStore 中最近一次失败 Evidence：

```text
persisted STEP_FAILED
    ↓
Evidence resolver
    ↓
RetryDecisionEngine(source=process_recovery)
    ├─ allowed=True  → 恢复为 PENDING
    └─ allowed=False → 不盲目重放
```

对于只有旧 `error_code`、没有 Evidence 的历史 Step，恢复服务必须 fail-closed，
不能把缺失事实解释成 `request_sent=False`。

---

## 5. Evidence 与 EventStore 的最小承载方案

不修改 `ExecutionStateManager` schema。Worker 的 `STEP_FAILED` payload 增加可序列化
快照：

```json
{
  "error_code": "TIMEOUT",
  "failure_category": "TIMEOUT",
  "evidence": {
    "execution_id": "...",
    "step_id": "...",
    "invocation_id": "...",
    "request_sent": false,
    "side_effect_state": "NONE",
    "operation_id": "..."
  },
  "retry_decision": {
    "allowed": true,
    "reason": "...",
    "requires_reconciliation": false
  }
}
```

EventStore 已经支持 JSON payload；使用持久化 EventStore 时，Retry API 和
RecoveryService 可以读取同一事实。没有 Evidence snapshot 的旧事件必须走拒绝
分支。

---

## 6. Phase 10-G/H/I 的 Runtime 接口预留

### 6.1 ExternalOperationRecord

Phase 10-G 新增 Runtime 层记录，不修改 Java/Creator：

```text
operation_id
execution_id
step_id
tool_name
status
external_operation_id
receipt_id
idempotency_key
created_at
updated_at
```

状态：

```text
CREATED → SUBMITTED → PROCESSING → SUCCEEDED / FAILED
                                  └→ UNKNOWN
```

它负责保存“哪个逻辑 operation 需要被查询”，不负责自动重试或假设外部结果。

### 6.2 ReconciliationService

Phase 10-H 输入 `ExternalOperationRecord`，按
`external_operation_id` 优先、`receipt_id` 其次调用 Runtime 注入的查询器，
输出：

```text
SUCCEEDED | FAILED | NOT_FOUND | UNKNOWN
```

它只收敛外部事实，不直接修改 Execution 状态，不自动重新发布或补偿。

### 6.3 RetryTask 与 RetryScheduler

Phase 10-I 使用 Runtime 内部调度模型：

```text
RetryTask {
    execution_id
    step_id
    attempt
    next_retry_time
    backoff
    reason
}
```

Scheduler 负责：

- 保存延迟 Retry task；
- 到期取出 task；
- 检查 attempt budget 和 deadline；
- 通过同一个 RetryManager/Decision 边界执行；
- 以稳定 task key 防止同一 attempt 重复入队。

它不引入 MQ，也不在本 Sprint 中增加新的 Execution 状态。

---

## 7. 分阶段实现与 commit 边界

### Commit 1

```text
feat(runtime): add evidence aware retry decision
```

内容：

- `RetryDecision` 和 `RetryDecisionEngine`；
- Evidence resolver/event snapshot；
- Worker、RetryManager、resume、RecoveryService 统一经过 Decision；
- 最小安全矩阵测试。

### Commit 2

```text
feat(runtime): add external operation tracking
```

内容：

- `ExternalOperationRecord`；
- Runtime operation store/tracker；
- Worker 观察可能副作用和外部 operation evidence；
- 外部 operation 记录测试。

### Commit 3

```text
feat(runtime): add reconciliation foundation
```

内容：

- `ReconciliationService`；
- operation/receipt 查询接口；
- `UNKNOWN → SUCCEEDED/FAILED/NOT_FOUND/UNKNOWN` 映射；
- 外部成功但 Runtime 失败的测试。

### Commit 4

```text
feat(runtime): add retry scheduler
```

内容：

- `RetryTask`；
- 延迟取 due task；
- budget/deadline 检查；
- 同一 attempt 的幂等入队保护；
- scheduler 测试。

每个 commit 只包含对应阶段的文件和测试，不提交现有工作树中的无关修改。

---

## 8. 验收场景

必须覆盖以下通用场景：

1. `request_sent=False + NONE`：Decision 允许 Retry；
2. `request_sent=None + UNKNOWN`：Decision 拒绝 Retry；
3. `request_sent=True + POSSIBLE`：Decision 要求 reconciliation；
4. 外部服务已经成功但 Runtime 收到失败：Reconciliation 返回
   `SUCCEEDED`，不发起重复写操作；
5. 同一 operation/attempt 重复调用 Retry API 或 scheduler：只产生一个有效
   Retry task，不重复放行。

本设计完成后，进入 Commit 1 的最小实现；不进入 Legacy Cleanup。

