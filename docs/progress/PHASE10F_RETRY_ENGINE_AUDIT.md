# Phase 10-F：Retry Engine 当前能力审计

## 0. 审计范围与结论

本文件只完成 Phase 10-F Step 1 的只读审计，依据当前代码和已有迁移文档确认
Retry 的真实行为。本文不设计或实现 `RetryDecision`，不修改业务代码、Execution
状态模型、外部服务或测试。

当前 Runtime 已经能够在一次执行调用内形成并消费失败事实：

```text
ToolRuntime
    ↓
InvocationResult / ExecutionResult
    ↓
ExternalAgentFailure
    ↓
FailureDecisionEngine
    ↓
FailureClassifier + FailurePolicy
    ↓
Worker
```

但是当前 Retry 仍然是旧的、以状态和错误码为中心的恢复机制：

```text
StepExecution.error_code
    + StepExecution.status
    + retry_count / max_retries
    ↓
RecoveryPolicy
    ↓
RetryManager / resume / process recovery
    ↓
StepExecution → PENDING
```

核心结论：

> 当前已有的是“重新打开失败 Step”的能力，不是基于 Execution Evidence 的安全
> Retry Engine。`RetryManager`、`ExecutionRecoveryService` 和 `resume_execution`
> 都不能读取或验证 `request_sent`、`side_effect_state`、receipt、operation identity
> 和幂等证据，因此现在不能安全地对通用外部操作执行自动 Retry。

特别需要保留的事实是：`ExecutionEvidence` 已经能够在当前调用边界承载足够多的
证据，但这些证据还没有到达现有 Retry 的持久化/消费边界。

---

## 1. 当前 Retry 从哪里触发

### 1.1 Worker 失败分支：只产生 Retry 候选状态，不立即重新调用

真实入口是 `ExecutionWorker._execute_one_step()`：

```text
CapabilityExecutor.execute_step()
    ↓
ExecutionResult(ok=False)
    ↓
Worker._failure_from_result()
    ↓
FailureDecisionEngine.decide()
```

`_failure_from_result()` 优先使用 `ExecutionResult.external_failure`；没有该对象时，
才从兼容的 `tool_result`、`error_code`、`retryable` 和 `request_sent` 字段重新构造
失败事实。随后 Worker 组装 `FailurePolicyContext`，其中包含当前 attempt、剩余
`retry_budget`、capability、tool name，以及从失败事实得到的副作用和幂等摘要。

当前分支行为如下：

1. `REQUEST_USER_INPUT`：暂停 Execution，返回 `PAUSED`。
2. `decision.retry_allowed=True`：写入 `STEP_FAILED(retryable=True)`，调用
   `ExecutionStateManager.fail_step()`，将 Step 置为 `FAILED_RETRYABLE`，或者在
   达到 `max_retries` 后置为 `FAILED`。
3. 其他失败：写入 `STEP_FAILED(retryable=False)`，以 `permanent=True` 失败，并将
   下游 Step 标记为 `SKIPPED`。

这个分支不会在同一次 `Worker.run()` 中立即再次调用 Tool。真正的下一次调用需要
后续的 `RetryManager.retry_step()`、`resume_execution()` 或另一个 Worker pass。

当前还有一个需要在后续统一的兼容性问题：`FailurePolicy` 目前只产生
`FAIL_FAST` 或 `REQUEST_USER_INPUT` 两种 `RecoveryDecision.action`，但它仍可能把
旧的 `retry_allowed` 设为 `True`。Worker 在处理完 `REQUEST_USER_INPUT` 后依据
`retry_allowed` 进入 `FAILED_RETRYABLE` 分支，所以可能出现：

```text
RecoveryDecision.action = FAIL_FAST
RecoveryDecision.retry_allowed = True
Worker 实际写入 FAILED_RETRYABLE
```

这说明当前 `retry_allowed` 只是 Phase 10-D 的兼容信号，不是完整 Retry 决策，也
不是可直接授权外部重放的安全结论。

### 1.2 显式 Step Retry API 与 `RetryManager`

当前 API 只有 Step 级 Retry 入口：

```text
POST /executions/{execution_id}/steps/{step_id}/retry
    ↓
runtime_routes.retry_execution_step()
    ↓
RetryManager.retry_step()
```

API 会先做执行存在性、控制权限和授权检查，然后调用 `RetryManager`。该 Manager
的真实行为是：

1. 按 `step_id` 或 `step_execution_id` 找到 Step。
2. 发出 `STEP_RETRY_REQUESTED`，原因只取 `step.error_code`。
3. 调用 `RecoveryPolicy.can_retry(step)`。
4. 通过后调用 `ExecutionStateManager.retry_step()`，把 Step 从 `FAILED` 或
   `FAILED_RETRYABLE` 重置为 `PENDING`。
5. 保存只包含完成 Step、当前 Step 和调用方 snapshot 的 checkpoint。
6. 发出 `STEP_RETRY_STARTED`。
7. 返回 Step；它本身不执行 Tool，不调用 `FailureDecisionEngine`，也不读取
   `ExecutionEvidence` 或 ToolRuntime ledger。

如果策略不允许，Manager 通常返回原 Step；若错误码属于旧的 retryable 白名单，
才额外发出 `STEP_RETRY_EXHAUSTED`。API 随后因为 Step 不是 `PENDING` 或 `RUNNING`
而返回 `409`。

因此，这个接口是“用户或调用方请求重新排队”的旧控制接口，不是安全 Retry API。
目前它没有以下检查：

- `request_sent` 三态；
- `side_effect_state`；
- receipt、operation id 或外部资源状态；
- runtime/external idempotency key；
- ToolContract 的副作用和幂等元数据；
- execution deadline、backoff 或 `retry-after`；
- 是否必须先做 reconciliation 或用户确认。

### 1.3 `resume_execution()` 以及再次调用 Worker

`ExecutionStateManager.resume_execution()` 会把 `FAILED_RETRYABLE` Step 重置为
`PENDING`，同时清空 `error_code` 和 `error_message`。`ExecutionWorker.run()` 在
普通执行 pass 开始时，会对非 `PAUSED`/`WAITING_APPROVAL` 的 Execution 调用
这个 resume 逻辑。因此，调用方只要再次启动 Worker pass，也可能触发下一次 Tool
调用，而不经过 `RetryManager` 的 Evidence 检查。

同样，已有的 `POST /executions/{execution_id}/resume` 也会进入 Runtime 的
`resume_execution()`。这条接口是用户控制生命周期的接口，但在当前状态模型中也
承担了重置 `FAILED_RETRYABLE` Step 的效果。

### 1.4 进程恢复路径

`ExecutionRecoveryService.restore_execution()` 会扫描处于可恢复 Execution 状态的
执行，或包含 `FAILED_RETRYABLE` Step 的执行。对满足旧 `RecoveryPolicy` 的 Step，
它直接调用 `state.retry_step()`，把 Step 重新置为 `PENDING`。

这是一条进程恢复/重启路径，不是新的 Retry Engine；它同样不读取 Evidence。尤其
是，状态为 `RUNNING` 的中断 Step 也会通过 `recover_step()` 被重置为 `PENDING`，
当前状态模型无法表达“进程中断但外部结果可能已经产生”。

### 1.5 当前没有的触发能力

当前没有发现独立的：

- Retry scheduler；
- backoff timer 或 `retry_after` 调度；
- execution-level Retry API；
- 统一 Retry Decision service；
- 以 operation/receipt 查询为前提的 Retry gate。

所以当前所谓 Retry 是多个旧入口对同一 Step 做 `FAILED* → PENDING` 的重置，之后
再由 Worker 重新执行，而不是一个统一的“事实 → 安全决策 → 调度”流程。

---

## 2. 当前 Retry 判断依据

### 2.1 `RecoveryPolicy`：状态 + 错误码 + 次数

代码位置：

```text
packages/assistant_core/greenbook_assistant_core/execution/recovery.py
```

当前默认白名单是：

```text
TIMEOUT
NETWORK_ERROR
RATE_LIMIT
TEMPORARY_UNAVAILABLE
```

`can_retry(step)` 只有同时满足以下条件才返回 `True`：

```text
step.status ∈ {FAILED, FAILED_RETRYABLE}
error_code ∈ DEFAULT_RETRYABLE_CODES
retry_count < max(0, step.max_retries)
```

它不读取 `ExecutionResult.retryable`、`ExternalAgentFailure` 或
`ExecutionEvidence`，也不读取：

- `request_sent`；
- `side_effect_state`；
- `receipt_id`；
- `operation_id` / `external_operation_id`；
- `runtime_idempotency_key` / `external_idempotency_key`；
- ToolContract 的 `has_side_effect`、幂等能力或 RetryPolicy；
- deadline、backoff 和下游 `retry-after`。

因此 `RecoveryPolicy` 仍然是历史的 error-code gate，不是 Phase 10-B 设计的完整
FailurePolicy，也不是 Evidence-aware policy。

### 2.2 `retryable` 字段的实际语义

当前存在多处 `retryable`：

| 位置 | 当前语义 | 是否给 `RetryManager` 授权 |
|---|---|---:|
| MCP/ToolResult | 下游给出的候选提示 | 否 |
| ExternalAgentFailure | normalizer 保留的事实/候选属性 | 否 |
| FailureClassification | 结合类别和 side-effect 后的候选标记 | 否 |
| RecoveryDecision.retry_allowed | Phase 10-D 兼容性 eligibility 信号 | 否，尚未是完整 RetryDecision |
| StepExecution.retry_count/max_retries | 当前状态机的次数边界 | 仅表示次数，不表示安全性 |

`RetryManager` 实际只使用存储在 Step 上的 `error_code`、状态和次数。也就是说，
即使一次调用产生了完整 Evidence，进入 RetryManager 后目前也会退化成旧的错误码
判断。

另外，`ExecutionResult.request_sent` 仍是为旧调用者保留的兼容字段，虽然类型支持
三态，但默认值是 `False`。正常的 Phase 10-E 路径应以
`ExecutionEvidence.request_sent` 或 `ExternalAgentFailure` 中的事实为准；未来的
Retry 入口不能因为读取了这个 legacy 默认值，就把“未知投递”解释成“明确未发送”。

### 2.3 FailureDecisionEngine 当前提供的内容

Worker 当前已经调用 `FailureDecisionEngine`，这是 Phase 10-D 的接入成果。它能
根据完整失败事实完成：

- `FailureCategory` 分类；
- side-effect 风险判断；
- `request_sent` 的保留；
- `requires_reconciliation` 和 `human_required` 摘要；
- attempt 和剩余 retry budget 的上下文输入。

但当前 `FailurePolicy` 的 retry 分支仍然依赖旧的
`retry_eligibility(raw_error_code)` callback，并且 `idempotent` 当前是通过是否存在
一个 idempotency key 的布尔摘要得到的。这不能证明外部服务真正支持幂等，也不能
证明该 key 在下一次调用时仍然有效。

FailureDecisionEngine 目前还没有输出：

- backoff strategy；
- retry-after 或下一次允许时间；
- operation 级最大尝试次数；
- evidence requirements；
- 外部状态查询前置条件；
- 用户确认和 reconciliation 的完整执行门。

### 2.4 当前策略存在的通用漂移

`FailureClassifier` 可以把一组下游错误归入 `DEPENDENCY_UNAVAILABLE`，但
`RecoveryPolicy` 仍按原始错误码精确匹配旧白名单。例如当前白名单不包含通用的
`DEPENDENCY_UNAVAILABLE` 类别。这个差异说明分类层、策略层和旧 RetryManager 还
没有统一到同一个 Retry Policy；不应通过为某个下游错误码添加特殊分支来解决。

---

## 3. 为什么当前不能安全 Retry

### 3.1 Retry 边界拿不到原始 Evidence

Phase 10-E 已经补充了 `ExecutionEvidence`，当前调用内的数据流是：

```text
ToolInvocationContext
    ↓
ToolRuntime / ToolExecutionLedger
    ↓
InvocationResult.evidence
    ↓
RuntimeAgentService adapter
    ↓
ExecutionResult.evidence / external_failure.evidence
    ↓
Worker FailureDecisionEngine
```

但是在 Worker 写入失败状态后：

- `StepExecution` 只持久化 `error_code`、`error_message`、状态、次数和时间；
- `ExecutionStateManager` schema 没有 Evidence 或 evidence reference；
- `STEP_FAILED` 当前 payload 只有错误、分类、动作和原因摘要，没有完整 envelope；
- `RuntimeManager.save_checkpoint()` 只保存完成 Step、当前 Step 和调用方 snapshot，
  没有 invocation evidence；
- RuntimeAgentService 当前执行路径创建的 `ToolExecutionLedger` 是本次 Runtime
  生命周期内的对象，API 重新创建 `RetryManager` 时不能直接访问它；
- 进程恢复后只剩持久化的 Step 状态和旧错误码。

因此，第一次调用时分类器可能看到 `request_sent=None` 和
`side_effect_state=UNKNOWN`，但真正执行 Retry 的 `RetryManager` 只能看到
`TIMEOUT` 或其他 `error_code`。

### 3.2 旧 Retry gate 可能重放已经越过外部边界的写操作

当前 `RecoveryPolicy` 不检查副作用证据。于是以下事实都可能被旧逻辑忽略：

```text
request_sent=True
side_effect_state=POSSIBLE / UNKNOWN
```

只要 error code 在白名单、次数未耗尽，显式 Retry API 就可能把 Step 重新置为
`PENDING`。对于发布、创建、更新、取消等写操作，这可能导致重复操作。

即使：

```text
request_sent=None
side_effect_state=UNKNOWN
```

也不能把它当成未发送。`None` 的含义是无法确认投递，必须按保守风险处理；当前
RetryManager 没有这条安全门。

### 3.3 `FAILED` 不是“没有副作用”的证明

当前 `RetryManager` 的 `can_retry()` 同时接受 `FAILED` 和 `FAILED_RETRYABLE`。只要
错误码在白名单、次数未达到上限，某些已经以 `permanent=True` 记录为 `FAILED` 的
Step 仍可能被重新置为 `PENDING`。

所以 `FAILED` 只代表当前执行状态已失败，不代表：

- 请求从未发送；
- 外部事务没有提交；
- 结果一定没有产生；
- 可以安全地用同一逻辑操作重放。

### 3.4 没有 operation-level identity 和外部状态查询

`ExecutionEvidence.operation_id`、`external_operation_id`、`receipt_id` 当前可以
承载上游提供的值，但：

- Runtime 不保证跨 attempt 生成和保存一个稳定的逻辑 operation id；
- RetryManager 不消费这些字段；
- 没有统一的 external operation status/query 接口；
- 没有 receipt/artifact 查询作为重放前置条件；
- 没有 reconciliation 结果可以把 UNKNOWN 收敛为未执行、已执行或人工无法确认。

没有这些能力，Retry 只能是盲目 replay，而不是对原操作事实进行安全决策。

### 3.5 没有时间和预算之外的调度约束

当前已有 `max_retries` 和 `retry_count`，但缺少：

- 指数/固定 backoff；
- jitter；
- `Retry-After` 或依赖恢复时间；
- execution deadline 与单次 timeout 的组合判断；
- tenant/tool 级速率限制；
- 重启后下一次可执行时间。

因此即使某个错误码暂时可重试，当前接口也只能立即把 Step 重置为 `PENDING`。

### 3.6 多条恢复入口没有共用同一安全决策

现在至少有以下独立路径：

```text
Worker FailureDecisionEngine → FAILED_RETRYABLE
RetryManager → RecoveryPolicy → PENDING
ExecutionRecoveryService → RecoveryPolicy → PENDING
resume_execution / Worker.run → PENDING
```

其中只有第一条路径在失败发生时看到 `ExternalAgentFailure`；后三条只看到持久化
状态和错误码。未来 Retry Engine 必须先统一这些决策边界，否则同一个失败会因触发
入口不同而得到不同安全结果。

---

## 4. 当前 Evidence 已经可以支持什么

### 4.1 当前调用边界可获得的证据

`ExecutionEvidence` 和 Phase 10-E 适配层已经能在一次调用尚未离开内存之前承载：

| Evidence | 当前状态 | 对未来 Retry 的价值 |
|---|---|---|
| `execution_id` / `step_id` / `invocation_id` | Runtime context 和 ToolRuntime 可产生 | 关联一次 Execution、Step 和 attempt |
| `tool_call_id` | RuntimeAgentService 的 MCP adapter 可注入 | 关联一次 MCP 调用 |
| `operation_id` | 模型字段存在，但不会自动生成 | 只有上游提供时才可作为逻辑操作身份 |
| `request_hash` / `request_time` | `ExecutionEvidence.from_context()` 产生 | 比较尝试是否针对同一请求，并提供时间线 |
| `request_sent` | 保持 `False`/`True`/`None` 三态 | 判断是否越过外部边界；未知不能乐观解释 |
| `side_effect_state` | 可保留 `NONE`、`NOT_STARTED`、`POSSIBLE`、`UNKNOWN` 等事实 | 区分可候选重试和必须先收敛结果的调用 |
| `receipt_id` | 上游提供时可传递 | 关联回执、查询或已产生的外部结果 |
| `external_operation_id` / `resource_refs` | 上游提供时可传递 | 定位外部 operation 或资源 |
| runtime/external idempotency key | 已分成独立字段；外部 key 需上游提供 | 证明同一操作重放是否可能去重 |
| `status_code` / `raw_error_type` / `phase` / `trace_id` | 有 envelope 字段，值依赖调用层提供 | 区分连接、写入、读取和响应阶段 |

这些证据足以为未来的安全决策提供输入，特别是能够区分“明确未发出”和“结果
未知”。但它们目前只在当前调用链或当前内存 ledger 中可见。

### 4.2 当前 ledger 的边界

`ToolExecutionLedger` 的 `LedgerEntry` 已经保存 invocation identity、状态、时间、
结果/错误和 `evidence`。它的 `try_replay()` 只会返回已完成调用的缓存结果；对于
`FAILED`/`TIMEOUT`，它明确返回 `None`，把“是否重试”的责任留给调用方。

这保证了 ledger 不会把失败结果自动当作可重放，但目前也存在两个限制：

1. ledger 是 ToolRuntime 生命周期内的内存对象，不是跨进程/重启可查询的 operation
   evidence store；
2. `RetryManager` 没有 ledger 查询依赖，因此无法利用其中的 Evidence 作安全门。

### 4.3 当前 Evidence 对安全矩阵的实际支持程度

从事实语义看，未来只有下列组合可能成为自动 Retry 候选，还必须满足暂时性错误、
预算、deadline 和工具幂等条件：

```text
request_sent=False + side_effect_state=NONE
request_sent=False + side_effect_state=NOT_STARTED
```

以下组合不能被当前 Runtime 当作自动 Retry 候选：

```text
request_sent=False + POSSIBLE/UNKNOWN
request_sent=True  + POSSIBLE/UNKNOWN/CONFIRMED
request_sent=None  + POSSIBLE/UNKNOWN
```

当前问题不是这些安全语义没有被设计，而是 `RetryManager` 和恢复服务没有拿到并
执行这张矩阵。现有代码可能仅凭 error code 放行上述不安全组合。

---

## 5. 当前 Retry 仍缺失的 Evidence 与基础能力

下列项目是进入真正 Retry Engine 前的缺口；本阶段不实现它们。

### 5.1 持久化和可检索性缺失

- 每次 attempt 的完整 Evidence 不能从 `execution_id + step_id` 稳定检索；
- `StepExecution` 只有错误摘要，没有 evidence reference；
- `STEP_FAILED`/`STEP_RETRY_*` 事件没有 attempt-level evidence 快照；
- API Retry 和进程恢复不能复用原始 invocation 的证据。

### 5.2 外部操作事实缺失

- `operation_id` 没有统一生成、生命周期和跨 attempt 规则；
- external operation status/query 未统一；
- receipt、external reference 和 resource state 没有强制契约；
- `UNKNOWN` 结果没有 reconciliation 结果来源。

### 5.3 幂等安全缺失

- runtime key 与 external key 已分字段，但没有确认外部服务实际使用 external key；
- key 的 TTL、scope 和跨 attempt 复用规则未定义；
- 当前 Worker 以“存在 key”近似 `idempotent=True`，这不是幂等能力证明；
- RetryManager 没有检查 ToolContract 是否允许安全重放。

### 5.4 策略和调度缺失

- 当前 `RecoveryPolicy` 仍是错误码白名单；
- 分类结果和旧 raw-code whitelist 存在漂移；
- 没有 backoff、jitter、retry-after、deadline 和 next-attempt time；
- 没有将 retry budget、工具策略和租户策略统一组合的决策对象；
- 没有把“需要对账”和“允许重试”建模为强制互斥的安全门。

---

## 6. 对现有 Execution 状态模型的影响

本阶段不修改状态模型。当前相关状态仍然是：

```text
StepStatus:
PENDING → RUNNING → FAILED_RETRYABLE / FAILED
FAILED / FAILED_RETRYABLE → PENDING   (显式 retry 或 resume)
```

当前模型没有：

- `WAITING_RETRY`；
- `WAITING_DEPENDENCY`；
- `UNKNOWN_RESULT`。

这意味着 `FAILED_RETRYABLE` 目前同时承担“下一次可以由调用方重新运行”和“旧恢复
机制认为错误码可重试”的含义，不能精确表示：

- 已经通过 Evidence 安全检查的 retry candidate；
- 等待 backoff 的 Step；
- 依赖不可用而等待的 Step；
- 外部结果未知、禁止重放的 Step。

在没有新的状态设计之前，不应把现有 `FAILED_RETRYABLE` 扩展为未知结果或依赖等待
状态，也不应通过改变 `ExecutionStateManager` schema 掩盖 Evidence 缺口。

---

## 7. 审计结论：对五个问题的直接回答

### 1. 当前 retry 从哪里触发？

- Worker 失败分支只将部分失败标记为 `FAILED_RETRYABLE`，不在同一 pass 立即重试；
- Step Retry API 通过 `RetryManager.retry_step()` 将失败 Step 重置为 `PENDING`；
- `resume_execution()` 和下一次 Worker pass 也会重置 `FAILED_RETRYABLE`；
- `ExecutionRecoveryService` 在进程恢复时按旧策略重置 Step；
- 当前没有独立 Retry scheduler 或 backoff engine。

### 2. 当前 retry 判断依据是什么？

主要是：

```text
StepStatus
error_code 是否在旧白名单
retry_count < max_retries
```

Worker 另有 Phase 10-D 的分类和 `retry_allowed` 兼容信号，但显式 RetryManager 和
进程恢复服务不消费 FailureDecision 或 Evidence。

### 3. 为什么现在不能安全 retry？

因为执行 Retry 的入口只能拿到 error code/status/count，拿不到或不验证：

```text
request_sent
side_effect_state
receipt / operation identity
external resource state
idempotency evidence
deadline / backoff / ToolContract safety
```

因此可能对已发送、可能产生副作用或结果未知的写操作执行盲目 replay。

### 4. 哪些 evidence 已经可以支持 retry？

当前调用边界已经可以提供：

- invocation identity 和 tool call identity；
- request hash/time；
- `request_sent` 三态；
- `side_effect_state`；
- receipt、external operation/resource reference（上游提供时）；
- runtime/external idempotency key（各自有值时）；
- response/transport/phase/trace 信息。

这些证据可以支持未来做安全候选判断，但尚不能支持当前 RetryManager 直接执行
Retry。

### 5. 哪些 evidence 缺失？

- 跨 retry、进程和服务重启可检索的持久 Evidence；
- 稳定的 operation-level identity；
- 外部 operation 查询和 reconciliation 结果；
- event-level attempt evidence；
- 可验证的 external idempotency contract、scope 和 TTL；
- backoff、deadline、retry-after 及统一 policy context；
- 所有 Retry 入口共用的 Evidence-aware decision boundary。

---

## 8. Phase 10-F Step 2 的进入条件（仅记录，不实施）

在进入下一步 RetryDecision 设计前，至少需要确认：

1. RetryDecision 必须消费完整失败事实、FailureClassification、执行上下文和
   Evidence，而不是只接收 `error_code`；
2. `request_sent=None`、`POSSIBLE` 和 `UNKNOWN` 必须默认 fail-closed，不能因为
   错误码属于 transient 类别就放行；
3. 显式 Retry API、Worker 后续 pass、resume 和进程恢复必须使用同一决策边界；
4. Evidence 的持久化/检索方案必须与“禁止本阶段修改 ExecutionStateManager schema”
   的约束相容；
5. 没有 operation identity、外部状态查询或可靠幂等证据时，写操作不能自动重放；
6. backoff、deadline、retry budget 和 ToolContract 安全元数据必须在决策时可用；
7. Retry Engine 的实现必须后置于决策设计，不应先把现有 `RetryManager` 的白名单
   放宽。

这些是后续设计的前置审计结论，不是本阶段的 `RetryDecision` 实现方案。本阶段到
此停止，不新增 Retry、等待、对账或 Execution 状态。

---

## 9. 审计依据

本次只读检查的主要代码和文档：

- `packages/assistant_core/greenbook_assistant_core/execution/recovery.py`
- `packages/assistant_core/greenbook_assistant_core/execution/retry_manager.py`
- `packages/assistant_core/greenbook_assistant_core/execution/recovery_service.py`
- `packages/assistant_core/greenbook_assistant_core/execution/worker.py`
- `packages/assistant_core/greenbook_assistant_core/execution/state_manager.py`
- `packages/assistant_core/greenbook_assistant_core/execution/models.py`
- `packages/assistant_core/greenbook_assistant_core/execution/failure_decision.py`
- `packages/assistant_core/greenbook_assistant_core/execution/evidence.py`
- `packages/assistant_core/greenbook_assistant_core/execution/invocation.py`
- `packages/assistant_core/greenbook_assistant_core/execution/runtime/ledger.py`
- `packages/assistant_core/greenbook_assistant_core/execution/runtime/tool_runtime.py`
- `apps/assistant_api/greenbook_assistant_api/api/runtime_routes.py`
- `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py`
- `packages/assistant_core/greenbook_assistant_core/execution/persistence.py`
- `packages/assistant_core/greenbook_assistant_core/execution/postgres_repository.py`
- `docs/progress/PHASE10B_FAILURE_POLICY_PLAN.md`
- `docs/progress/PHASE10E1_EVIDENCE_ENVELOPE_IMPLEMENTATION.md`
