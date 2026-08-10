# Phase 10-E-1：Execution Evidence Envelope 审计

## 0. 审计范围

本文件只完成 Phase 10-E-1 的 Step 1：审计当前执行证据在 Runtime 各层之间的存在位置和丢失位置。

本阶段不实现 `ExecutionEvidence`，不修改执行流程，不增加 Execution 状态，也不实现 Retry、WAITING_DEPENDENCY、Reconciliation 或自动恢复。Step 2 的统一结构设计和 Step 3 的最小实现等待本文件确认后再执行。

审计依据：

- `docs/progress/PHASE10A_FAILURE_CLASSIFICATION_PLAN.md`
- `docs/progress/PHASE10B_FAILURE_POLICY_PLAN.md`
- `docs/progress/PHASE10C_FAILURE_DECISION_RUNTIME_INTEGRATION_PLAN.md`
- `docs/progress/PHASE10E0_EXECUTION_EVIDENCE_AUDIT_PLAN.md`
- 当前 Phase10-D 代码实现及相关 Runtime、MCP、ToolResult 测试

审计结论先行：当前 Runtime 有若干分散的调用元数据，但还没有一个可以从工具调用一直传递到 Worker 的统一证据对象。当前的 `ExternalAgentFailure` 是失败事实的规范化模型，不是完整的执行证据包；`RecoveryDecision` 是策略决策，也不是证据包。

---

## 1. 当前真实数据流

### 1.1 主调用链

当前 Runtime 的单步调用链如下：

```text
PlanStep
  ↓
CapabilityExecutor
  ↓ 生成 ToolInvocationContext
ToolRuntime.invoke(context)
  ├─ ToolExecutionLedger.record_start(context)
  └─ RuntimeAgentService.raw_handler
       ↓ 生成 tool_call_id
     GreenBookMCPServer.execute_tool(...)
       ↓
     MCP ToolResult / raw dict
  ↓
InvocationResult
  ↓ RuntimeAgentService.invoke_fn 的有限字典适配
CapabilityExecutor
  ↓
ExecutionResult
  ├─ tool_result
  └─ external_failure（失败时的 transient 事实）
  ↓ Worker._failure_from_result
ExternalAgentFailure
  ↓
FailureDecisionEngine
  ├─ FailureClassifier
  └─ FailurePolicy
  ↓
RecoveryDecision
  ↓
Worker 消费并写入 STEP_FAILED / Execution 状态
```

### 1.2 各层的真实责任

| 层 | 当前真实作用 | 是否拥有完整证据 |
| --- | --- | --- |
| `CapabilityExecutor` | 绑定 PlanStep 参数，创建 `ToolInvocationContext`，把工具返回结果转换为 `ExecutionResult` | 否；只在转换失败时调用 `normalize_failure_payload`，没有独立证据对象 |
| `ToolInvocationContext` | 保存一次 Runtime 工具调用的执行、步骤、工具、参数、幂等和超时上下文 | 部分；有上层身份和原始参数，但没有 `tool_call_id`、发送阶段、响应和副作用事实 |
| `ToolRuntime` | 执行 handler、控制 Runtime timeout、维护内存 ledger 和异步结果 | 否；`InvocationResult` 是有限结果模型，timeout/exception 不能确认下游送达 |
| `ToolExecutionLedger` | 记录一次 invocation 的身份、状态、耗时、错误和部分 raw result | 否；仅内存，且没有请求摘要、送达状态、响应证据或 operation 级记录 |
| `RuntimeAgentService` | 创建 raw handler、生成 MCP `tool_call_id`、把 InvocationResult 压缩成 CapabilityExecutor 可消费的字典 | 是当前最明显的适配丢失点；没有原样传递 evidence |
| MCP Server | 校验工具、创建 `ToolContext`、调用 handler、返回 `ToolResult` 形状的字典 | 部分；某些分支有 state，异常和未知工具分支不完整 |
| `ExternalAgentFailure` | 把失败结果规范化为 dependency、error、request_sent、side_effect_state 等失败事实 | 部分；能保留传入的字段，但无法恢复上游已丢失的字段 |
| `FailureDecisionEngine` | 读取失败事实，调用 classifier 和 policy，返回 `RecoveryDecision` | 否；它消费证据，不产生或持久化完整证据 |
| Worker | 组织 step 生命周期、消费决策、写失败事件和 Execution 状态 | 否；当前事件只记录错误和决策摘要，不记录完整调用证据 |

---

## 2. 当前字段盘点

以下盘点针对用户指定字段：

```text
execution_id
step_id
tool_call_id
request_sent
side_effect_state
error_code
receipt
operation_id
idempotency_key
```

### 2.1 字段传递总表

| 字段 | Tool 调用 / Context | ToolRuntime / InvocationResult | ExecutionResult | ExternalAgentFailure | FailureDecision / Worker | 当前结论 |
| --- | --- | --- | --- | --- | --- | --- |
| `execution_id` | `ToolInvocationContext.execution_id` 存在；`CapabilityExecutor` 在 Execution 创建后回填 executor | `ToolRuntime` 可从 Context 读取；LedgerEntry 保存；`InvocationResult` 没有该字段 | 没有该字段 | 没有该字段 | Worker 方法参数、`ExecutionEvent.execution_id` 存在；`RecoveryDecision` 没有该字段 | 上下文、账本和事件层存在，离开 ToolRuntime 结果后丢失 |
| `step_id` | Context 存在 | ToolRuntime trace 和 LedgerEntry 保存；InvocationResult 没有该字段 | 没有该字段 | 没有该字段 | Worker 的 `step_ex.step_id` 和事件存在；Decision 没有该字段 | 由 Worker 闭包上下文保留，不能从失败事实独立定位 |
| `tool_call_id` | RuntimeAgentService raw handler 每次生成；MCP `ToolContext` 接收；部分 Java 请求头使用 | Context、LedgerEntry、InvocationResult 都没有 | 没有 | 没有 | Decision、STEP_FAILED payload 没有 | 只存在于 MCP 调用边界及部分下游传输，未进入 Runtime 证据链 |
| `request_sent` | MCP/`ToolResult` 支持 `bool \| None`，但默认值是 `False`；不同 helper 各自设定 | `InvocationResult.request_sent` 是 `bool`；`from_tool_result()` 使用 `bool(raw.get(...))` | 支持 `bool \| None`，但通常已经接收到布尔化结果 | 支持三态并保留传入值；可根据 state 或 request_sent 推导 side effect | `FailureClassification` 复制该字段；RecoveryDecision 只有嵌套 classification；事件不保存 | 字段名存在，但三态语义在 ToolRuntime 处可能丢失 |
| `side_effect_state` | 不是 ToolResult 顶层字段；可能放在 `ToolResult.state` 中；MCP 仅部分分支提供 | InvocationResult 没有 `state` 或该字段 | 没有顶层字段；`tool_result` 只有在上游未丢失时才可能携带 | 有 `SideEffectState`，可读取显式 state 或从 request_sent 推导 | Classification 有 `side_effect_risk`；Worker 仅用于 `has_side_effect`，事件不记录 | 规范化层局部存在，但不是 lossless 贯穿字段 |
| `error_code` | MCP raw dict/ToolResult 有 `code` | InvocationResult 有 `error_code`；ToolRuntime timeout/exception 可覆盖为通用错误 | 有 `error_code` | 有原始 `error_code` | Classification/Decision/STEP_FAILED/StepExecution 都有错误码 | 主要可传递，但原始下游错误可能被包装或覆盖 |
| `receipt` / `receipt_id` | ToolResult 有 `receipt_id`；外部服务仅部分响应提供 | InvocationResult 没有；ToolRuntime ledger 也没有专门字段 | 没有 | 有 `receipt_id`，前提是原始 ToolResult 仍保留 | Worker 用 `bool(receipt_id)` 判断是否支持 reconciliation；事件不记录 | 成功或直接 normalize 时可能存在，经过 Runtime 适配后通常丢失 |
| `operation_id` | 没有统一字段；Creator task_id、async task_id 或外部资源 ID 可能只在 data 中 | Context、InvocationResult、LedgerEntry 没有 operation_id | 没有 | 没有 | FailureDecision、Worker、ExecutionEvent 没有 | 当前没有统一的逻辑操作身份 |
| `idempotency_key` | Context 有 Runtime key；MCP `ToolContext` 可生成业务 key；Java/Creator 部分请求会发送 | LedgerEntry 保存 Context 的 key；InvocationResult 没有 | 没有 | 只从 `state["idempotency_key"]` 读取；state 丢失时为空 | FailurePolicyContext 接收 failure 中的 key；Worker 以 key 是否存在近似 `idempotent`；事件不记录 | 存在多层 key，但没有在证据链中建立映射 |

### 2.2 其他相关字段

下列字段虽然不在用户指定清单中，但直接影响 Execution Evidence 完整性：

| 字段 | 当前状态 |
| --- | --- |
| `invocation_id` | `ToolInvocationContext`、LedgerEntry 和 `InvocationResult` 存在；`ExecutionResult`、ExternalAgentFailure、Worker 事件没有统一携带 |
| `request_hash` | 当前不存在；Context 保存原始 `tool_args`，但没有规范化参数摘要或哈希 |
| `request_time` | Context 有 `created_at`，Ledger 有 `started_at`；没有明确的下游请求发送时间 |
| `status_code` | ToolResult、InvocationResult、ExecutionResult 和 ExternalAgentFailure 没有统一字段；HTTP 客户端内部可观察但没有进入结果 envelope |
| `raw_error_type` | ToolRuntime 只保存通用错误消息；没有统一记录 connect/write/read timeout 或异常类型 |
| `external_operation_id` | 可能出现在 Creator task 或工具 data 中，但没有统一字段，失败路径也不保证保留 |
| `phase` | ExternalAgentFailure 可从 `ToolResult.state["phase"]` 读取；MCP 部分校验分支提供，Runtime 适配后通常丢失 |
| `trace_id` | Runtime Context、MCP ToolResult、ExternalAgentFailure、RuntimeResult 局部存在；InvocationResult、ExecutionResult、事件 payload 没有统一保留 |

---

## 3. 分层审计：字段在哪里丢失

### 3.1 Tool 调用与 ToolInvocationContext

`CapabilityExecutor` 在真正调用前创建 `ToolInvocationContext`。当前 Context 保存：

```text
invocation_id
task_id
execution_id
step_id
capability
tool_name
tool_args
user_id / tenant_id
idempotency_key
timeout_seconds
created_at
```

已知限制：

1. Context 没有 `tool_call_id`。该 ID 直到 RuntimeAgentService 的 raw handler 中才生成。
2. Context 没有 `operation_id`。`invocation_id` 只能表示一次尝试，不能表示跨重试的逻辑操作。
3. Context 保留原始 `tool_args`，但没有 request hash；其 `idempotency_key` 的哈希材料包含 task、execution、step、tool 名称，不包含工具参数。
4. Context 没有 request_sent、side_effect_state、response 或下游操作凭据，因为这些信息在调用前尚未观察到。

因此 Context 是“调用意图和路由上下文”，不是执行结果证据。

### 3.2 ToolRuntime 与 InvocationResult

ToolRuntime 的确会把 Context 的身份写入 `ToolExecutionLedger`，并维护调用开始、完成、失败或超时的内存记录。但是 `InvocationResult` 当前只有：

```text
ok
invocation_id
tool_name
data
error_code
error_message
retryable
request_sent: bool
duration_ms
replayed
status
pending
async_task_id
```

这里有三个明确的丢失点：

1. `from_tool_result()` 只读取有限字段，不读取 `state`、`trace_id`、`receipt_id`、`resource_refs`、phase、operation ID 或下游响应信息。
2. `request_sent` 在转换时使用 `bool(raw.get("request_sent", False))`，因此原始 `None` 会变成 `False`。
3. ToolRuntime 自己的 `asyncio.wait_for` timeout 和 handler exception 分支直接构造 `InvocationResult`，没有从 raw handler 获得任何送达或副作用证据，因此默认表现为未发送。

同步失败时 Ledger 的 `record_failure()` 也只稳定记录错误码、消息、时间和耗时；主路径不统一保存原始失败 payload。异步分支虽然有时把 raw result 写入 ledger，但仍然通过有限的 `InvocationResult` 传递给上层。

### 3.3 RuntimeAgentService 适配层

当前 `RuntimeAgentService` 的 raw handler：

```python
call_kwargs = {
    "trace_id": ctx.trace_id,
    "agent_run_id": ctx.run_id,
    "tool_call_id": str(uuid.uuid4()),
}
return await mcp.execute_tool(tool_name, **call_kwargs, **tool_args)
```

这说明 `tool_call_id` 在 MCP 调用时确实存在，但它不是 `ToolInvocationContext` 的一部分，也没有进入 Ledger 或 InvocationResult。

之后 `invoke_fn` 只返回：

```text
ok
code
data
user_message
retryable
request_sent
pending
async_task_id
```

被丢弃的字段包括：

```text
state / side_effect_state
phase
trace_id
receipt_id
resource_refs
tool_call_id
operation_id / external_operation_id
idempotency_key
invocation_id
status_code / raw error type / timeout phase
```

这是当前从 ToolRuntime 到 CapabilityExecutor 的主要证据压缩点。因为 `CapabilityExecutor` 接收到的已经是有限字典，之后的 `normalize_failure_payload()` 无法恢复这些字段。

### 3.4 ExecutionResult

当前 `ExecutionResult` 有：

```text
ok
capability
tool_name
tool_result
error_code
error_message
retryable
artifact
approval_required
request_sent: bool | None
pending
async_task_id
external_failure: ExternalAgentFailure | None
```

`external_failure` 是 Phase10-D 为 Worker 决策边界增加的 transient 失败事实，明确没有进入 Execution/StepExecution 持久化模型。

当前缺失：

- `execution_id`、`step_id`、`tool_call_id`、`invocation_id`；
- `side_effect_state` 顶层字段；
- `operation_id`、`external_operation_id`、receipt；
- `idempotency_key` 及幂等证据；
- request hash/time、status code、raw error type、phase；
- 统一的 evidence source 或版本。

`tool_result` 理论上可以承载任意字典，但在当前 Runtime 主路径中它来自 `invoke_fn` 的有限结果，不能作为完整 evidence envelope 使用。

### 3.5 ExternalAgentFailure

当前 `ExternalAgentFailure` 具有：

```text
dependency
error_code
retryable
user_visible_message
recovery_action
request_sent: bool | None
side_effect_state
message
phase
trace_id
receipt_id
idempotency_key
metadata
```

它能完成 Phase9-B/Phase10-D 的失败事实标准化，但存在两个边界：

1. 它只能保存传入的事实，不能从 `ExecutionResult` 或已经压缩的字典中恢复 `tool_call_id`、execution/step identity、operation ID 或 response evidence。
2. 如果 `state["side_effect_state"]` 缺失，normalizer 会按 `request_sent` 推导：`None → UNKNOWN`、`True → POSSIBLE`、`False → NOT_STARTED`。这使得上游的 `False` 语义错误会被正式固化为 `NOT_STARTED`。

此外，`idempotency_key` 只从 `state["idempotency_key"]` 读取。RuntimeAgentService 当前没有传递 `state`，所以 Worker 经常只能看到空幂等键，即使 Context 或 MCP/外部请求路径曾经使用过幂等键。

### 3.6 FailureDecision 与 Worker

`FailureClassifier` 会读取 `ExternalAgentFailure` 的 `error_code`、`request_sent`、`side_effect_state` 和 retryable hint，生成 `FailureClassification`。`FailurePolicyContext` 另外接收：

```text
attempt
retry_budget
execution_deadline
capability
tool_name
has_side_effect
idempotent
idempotency_key
supports_reconciliation
source
```

但是：

- `FailurePolicyContext` 没有 `execution_id`、`step_id`、`tool_call_id`、`operation_id` 或完整 response evidence。
- Worker 当前用 `bool(failure.idempotency_key)` 近似设置 `idempotent`，这只是“有键”的判断，不是对幂等契约的事实证明。
- `supports_reconciliation` 当前主要由 `bool(failure.receipt_id)` 推断；receipt 在前面的适配层丢失时，该判断会被错误削弱。
- `RecoveryDecision` 只有分类和动作结果，`request_sent` 与副作用风险在嵌套的 Classification 中间接存在，没有独立 evidence 引用。
- Worker 的 `STEP_FAILED` payload 目前记录错误码、分类、恢复动作和原因，没有保存 request_sent、side_effect_state、receipt、幂等键或调用身份。
- ExecutionStateManager/StepExecution 仍然只承载现有状态和错误字段；本阶段不修改其 schema。

因此 Worker 能做“根据已有失败事实作当前决策”，但不能在事件或后续执行中重建完整的外部调用事实。

---

## 4. 重点字段的具体丢失分析

### 4.1 `request_sent`

当前存在三种不同语义来源：

1. MCP `ToolResult` 的显式字段，支持 `False/True/None`，但默认值仍是 `False`。
2. 外部客户端 helper 根据异常类型直接选择 `False` 或 `True`。
3. ToolRuntime 在自身 timeout/exception 时没有底层发送证据，直接构造默认 `False` 的 InvocationResult。

从 MCP 到 InvocationResult 时，`None` 被 `bool(...)` 转为 `False`。于是以下情况可能被错误合并：

```text
连接前没有发送
下游写入后 Runtime 等待超时
handler 已调用下游但随后抛异常
MCP 没有提供 request_sent 字段
```

这不是字段不存在，而是三态事实在 Runtime 边界被破坏。

### 4.2 `side_effect_state`

当前 MCP 部分分支把副作用信息放在 `state` 中，例如 `side_effect_started` 或 `side_effect_state`。但是：

- InvocationResult 没有 `state`；
- RuntimeAgentService 的 invoke_fn 不返回 `state`；
- ExecutionResult 没有顶层副作用状态；
- STEP_FAILED 事件不记录副作用状态；
- 组合工具的局部状态没有 operation-level 聚合。

因此 ExternalAgentFailure 的 side effect 状态通常是根据被压缩后的 request_sent 推导，而不是从原始证据保真传递。

### 4.3 `receipt`

`ToolResult.receipt_id` 和部分 Java 成功响应能够承载 receipt，但 InvocationResult、LedgerEntry、ExecutionResult 和 RuntimeResult 都没有对应的统一字段。

失败 helper 也不保证保留 receipt；例如服务端可能已经返回 receipt 后，客户端在后续读取/映射失败时只返回通用错误。Worker 目前只把 receipt 是否存在用于 `supports_reconciliation`，没有把 receipt 写入失败事件。

### 4.4 `operation_id`

当前不存在统一的 `operation_id` 字段。现有可关联标识分别承担不同含义：

| 现有标识 | 实际含义 | 能否替代 operation_id |
| --- | --- | --- |
| `execution_id` | 一次 Runtime Execution | 不能；一个 Execution 可包含多个逻辑操作和步骤 |
| `step_id` | Plan 中的步骤 | 不能；同一步可能有多次尝试或多个外部子操作 |
| `invocation_id` | 一次 ToolRuntime 调用 | 不能；重试应属于同一逻辑操作 |
| `tool_call_id` | 一次 MCP/传输调用 | 不能；通常每次传输调用重新生成 |
| `async_task_id` / Creator task ID | 某个异步外部任务 | 只能作为 external operation reference，不能泛化为 Runtime operation ID |
| `idempotency_key` | 外部去重身份 | 可能关联逻辑操作，但当前有多层 key 且没有统一映射 |

`content.create_draft` 这类组合工具还可能同时产生 Creator task、Creator artifact 和 Java draft。当前没有一个 operation ID 把这些子操作归并为一条可查询的逻辑操作证据。

### 4.5 `idempotency_key`

当前至少存在两类键：

- ToolRuntime Context/ledger 的 invocation key：由 task、execution、step、tool 名称生成，主要用于 Runtime 内存去重。
- MCP `ToolContext.idempotency_key()` 生成的业务 key：按 conversation、operation 和 scope 生成，并传给 Creator/Java 的部分请求。

这两类 key 的关系没有进入 `InvocationResult`、`ExecutionResult` 或 `ExternalAgentFailure`。FailureNormalizer 只读取 `ToolResult.state` 中的 key，当前主适配路径又丢弃 state，所以 Worker 通常无法知道外部请求实际使用的业务 key。

因此当前不能证明：

```text
Runtime invocation key
    是否对应
外部业务 idempotency key
    是否在未来 Retry 中复用
    是否仍在外部服务 TTL 内有效
```

---

## 5. MCP 与外部边界的审计结果

### 5.1 MCP pre-execution 失败

输入校验失败和 handler 签名不匹配分支通常返回：

```text
request_sent = False
state.phase = PRE_EXECUTION_VALIDATION_FAILED
downstream_called = False
side_effect_started = False
```

这些分支在 MCP 内部证据相对明确，但未知工具分支没有完整的 `request_sent/state`，且组合工具不能仅凭当前子调用证明整个 operation 没有副作用。

### 5.2 MCP post-execution 或 handler 失败

输出 schema 校验失败分支会返回 `request_sent=True` 和 POST_EXECUTION 状态，说明 handler 已被调用。但 handler exception 分支固定返回 `request_sent=False`，无法排除 handler 已经调用 Creator/Java 后才抛错。

所以当前 MCP 的 `False` 不是统一的“无副作用证明”，而是多个不同层级异常的默认值。

### 5.3 Java / Creator 客户端结果

当前客户端可能把以下事实映射成不同的 `ToolResult` helper：

- connect failure：通常 `request_sent=False`；
- write/read timeout：部分路径 `True` 或 `RESULT_UNKNOWN`；
- 5xx/dependency unavailable：可能重新使用 `False`；
- 已收到响应但业务/映射失败：receipt、status 和原始阶段不一定保留。

这些差异在进入 ToolRuntime 后又会被有限 `InvocationResult` 模型进一步压缩。因此当前 Runtime 不能从统一字段判断：请求是否接收、是否处理、是否提交、响应是否丢失。

---

## 6. 当前 Worker 能看到什么

### 6.1 失败决策输入

如果 `ExecutionResult.external_failure` 存在，Worker 直接使用它；否则 Worker 从 `result.tool_result`、错误码、消息、retryable 和 request_sent 重新调用 `normalize_failure_payload()`。

这意味着 Worker 当前能可靠使用的主要是：

```text
error_code
error_message
retryable
request_sent（但可能已被布尔化）
side_effect_state（可能是推导值）
dependency
部分 phase / trace / receipt / idempotency_key
```

它不能从 `ExecutionResult` 独立获取：

```text
execution_id
step_id
tool_call_id
invocation_id
operation_id
request_hash
request_time
status_code
raw_error_type
external_operation_id
可靠的 response / receipt evidence
```

### 6.2 Worker 输出

当前 `STEP_FAILED` 事件 payload 主要包含：

```text
step_execution_id
retryable
error_code
error_message
failure_category
recovery_action
recovery_reason
```

当前事件没有包含：

```text
request_sent
side_effect_state
tool_call_id
invocation_id
operation_id
receipt_id
idempotency_key
request_hash
phase
response status / timeout phase
```

所以事件可以回答“Runtime 当时判定了什么”，不能回答“外部请求事实是什么以及该判定依据是什么”。

---

## 7. 审计结论与 Step 2 前置问题

### 7.1 当前真实状态

当前链路不是完全没有证据，而是证据分散在不同的 transient 对象中：

```text
Context 有 execution/step/invocation/idempotency
Ledger 有 execution/step/invocation/status/time
MCP 有 tool_call_id 和部分 state
ToolResult 有 request_sent/state/receipt
ExternalAgentFailure 有规范化失败事实
Worker 有 decision 和 execution event
```

但是这些对象之间没有一个 lossless 传递边界。最关键的压缩点是：

```text
MCP ToolResult
    ↓ 字段有限化、request_sent 布尔化
InvocationResult
    ↓ RuntimeAgentService 再次有限化
ExecutionResult
    ↓ transient failure only
ExternalAgentFailure / RecoveryDecision
    ↓ 事件只保存决策摘要
Worker
```

### 7.2 Step 2 之前必须明确的事实

后续统一 Envelope 设计至少需要解决这些事实关系：

1. `execution_id`、`step_id`、`invocation_id`、`tool_call_id` 和 `operation_id` 的身份层级不同，不能用一个字段替代全部。
2. `request_sent` 必须保持 `False/True/None` 三态，且 `None` 不能在任何适配层转为 `False`。
3. `side_effect_state` 必须作为独立事实传递，不能只从 request_sent 事后推导。
4. Runtime key 与 MCP/外部业务 idempotency key 必须能够关联，但不能假设它们天然相同。
5. receipt、external operation reference 和 response evidence 必须在失败路径也可传递，不能只在成功结果中存在。
6. 组合工具需要 operation-level 事实，否则单个叶子调用的状态不足以支持未来 Retry/Reconciliation。
7. `ExternalAgentFailure` 和 `RecoveryDecision` 应继续保持“失败事实”和“策略决定”的职责，不应把二者混成执行状态或重试命令。

### 7.3 当前阶段停止条件

本文件完成 Step 1 的只读审计。到此停止：

- 不新增 `ExecutionEvidence` 类；
- 不修改 `InvocationResult`、`ExecutionResult`、ToolRuntime 或 RuntimeAgentService；
- 不修改 MCP、Creator、Java 或 ExecutionStateManager；
- 不实现 Retry、等待、Reconciliation 或自动恢复；
- 不针对任何具体错误码写逻辑。

待用户确认本审计结果后，才进入 Step 2 的统一 Evidence Envelope 设计，再由用户确认是否进入 Step 3 最小承载能力实现。
