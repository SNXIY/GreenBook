# Phase 10-E-0：Execution Evidence 完整性审计设计

## 0. 范围与结论

本阶段是 Retry Engine 之前的只读审计，不修改业务代码、Execution 状态模型、MCP ToolContract 或外部服务，也不实现 Retry、Reconciliation、WAITING_DEPENDENCY 或新状态。

本审计以当前仓库代码和 Phase 10-A、10-B、10-C、10-D 文档为准，区分“代码已经保存并传递的事实”和“后续阶段需要补齐的设计”。

当前失败链路已经具备统一的分类与决策边界：

```text
Tool Failure
    ↓
ToolResult / InvocationResult
    ↓
ExternalAgentFailure
    ↓
FailureDecisionEngine
    ├─ FailureClassifier
    └─ FailurePolicy
    ↓
RecoveryDecision
    ↓
Worker 消费
```

核心结论是：Phase 10-D 已经能够让 Worker 消费 `RecoveryDecision`，但当前执行证据还没有形成一条完整、不可丢失、可跨重试和跨进程查询的证据链。因此现在还不具备对外部写操作安全实现自动 Retry 的条件。

最重要的安全边界如下：

> 缺失证据不能被解释为“没有副作用”。对于写操作，只要请求送达状态或副作用状态不能被证明，默认不得自动重放；必须进入后续 Reconciliation 或人工处理路径。

这不是对某一个错误码的特殊处理，而是所有外部操作的通用规则。

---

## 1. 当前执行失败证据链分析

### 1.1 实际调用路径

当前一次单步执行大致经过以下层次：

```text
Worker.execute step
    ↓
CapabilityExecutor
    ↓
RuntimeAgentService 的 invoke_fn
    ↓
ToolRuntime.invoke(ToolInvocationContext)
    ↓
RuntimeAgentService raw_handler
    ↓
MCPServer.execute_tool
    ↓
Creator Client / Java Client / 其他外部服务
```

失败返回时，路径反向传播：

```text
Creator / Java / MCP handler failure
    ↓
ToolResult
    ↓
InvocationResult
    ↓
RuntimeAgentService 的结果适配字典
    ↓
ExecutionResult
    ↓
FailureNormalizer
    ↓
ExternalAgentFailure
    ↓
FailureDecisionEngine
    ↓
Worker 的 STEP_FAILED 与 RuntimeResult
```

### 1.2 各层当前保存的信息

| 层 | 当前实际保存的信息 | 当前边界 |
| --- | --- | --- |
| `ToolInvocationContext` | `invocation_id`、`task_id`、`execution_id`、`step_id`、`capability`、`tool_name`、工具参数、`idempotency_key`、超时、创建时间 | 参数只在内存上下文中存在，没有持久化的规范化参数摘要；没有 `tool_call_id`、请求哈希、请求送达阶段 |
| `ToolExecutionLedger` | 调用身份、幂等键、工具和执行标识、PENDING/RUNNING/COMPLETED/FAILED/TIMEOUT、开始/结束时间、耗时、错误码和部分结果 | 仅为 Runtime 内存账本；没有请求哈希、请求参数、响应状态码、响应头、送达证据、副作用状态、操作凭据或超时阶段 |
| `ToolRuntime` | 创建账本记录、执行超时、异常和正常结果；从原始工具结果提取 `ok`、错误码、消息、`retryable`、`request_sent` | `InvocationResult` 将 `request_sent` 转为布尔值；外层超时和异常默认表现为未发送，无法证明底层是否已经发送；`state`、receipt、资源引用等没有进入结果 |
| Runtime raw handler / MCP context | 每次 MCP 调用生成 `tool_call_id`，并传入 trace、agent run、会话和认证上下文 | `tool_call_id` 只在这次调用及部分下游请求头中出现，没有进入账本、`InvocationResult`、`ExecutionResult` 或失败事件 |
| MCP Server | 输入校验失败可带 PRE_EXECUTION 阶段和 `request_sent=False`；输出校验失败可带 POST_EXECUTION 阶段和 `request_sent=True`；正常 `ToolResult` 可带 `state`、trace、receipt、resource refs | handler 异常统一返回 `request_sent=False`，不能排除已调用下游；不同工具和客户端对 `state` 的填充不一致；组合工具没有统一的操作级摘要 |
| Java / Creator Client | 传递部分 trace、幂等键和调用标识；部分超时返回 `TIMEOUT` 或 `RESULT_UNKNOWN`；成功时部分返回 receipt 或资源标识 | 失败辅助方法经常丢失 HTTP 状态码、receipt、外部操作 ID 和响应阶段；连接失败、服务端 5xx、写超时的“是否已接收”语义不统一 |
| `ToolResult` | `error_code`、消息、`retryable`、`request_sent`、可选 `state`、trace、receipt、resource_refs | 没有统一的 `source`、`operation_id`、`tool_call_id`、`request_hash`、请求/响应时间、HTTP 状态码和超时阶段；很多 helper 的默认值带有未经验证的假设 |
| `ExternalAgentFailure` | `dependency`、原始 `error_code`、`retryable`、请求送达三态、副作用状态、phase、trace、receipt、幂等键、`metadata` | 主要是一次失败的规范化事实，当前不是持久证据包；没有执行/步骤/工具调用身份和完整请求/响应证据；`side_effect_state` 可能是根据不充分的 `request_sent` 推导的 |
| `ExecutionResult` | `error_code`、错误消息、`retryable`、`request_sent: bool | None`，以及短生命周期的 `external_failure` | 没有 `side_effect_state`、`tool_call_id`、`invocation_id`、receipt、phase、响应摘要或操作级证据；`external_failure` 是 transient 字段，不是持久账本 |
| Worker / `ExecutionEvent` | Phase 10-D 的 `STEP_FAILED` 包含分类、恢复动作、决策原因、原始错误码和消息 | 当前事件没有完整 evidence envelope；执行状态仍主要保存错误信息和重试计数，不能仅凭事件重建一次外部调用的送达和副作用事实 |

### 1.3 用户要求字段的现状

| 字段 | 当前是否存在 | 真实传递情况与缺口 |
| --- | --- | --- |
| `error_code` | 部分完整 | MCP、`ToolResult`、`InvocationResult`、`ExecutionResult` 和决策层都有错误码。但组合工具可能把下游错误包装成 `INTERNAL_ERROR`，Java/Creator 客户端也可能把不同失败归并为不可用，原始错误码不总能保留。 |
| `source` | 不完整 | `FailurePolicyContext` 可以接收 source，`ExternalAgentFailure` 当前主要只有 dependency；没有统一的 source 字段贯穿 ToolResult、账本、ExecutionResult 和事件。实际来源只能从层级、工具名或错误码推断。 |
| `request_sent` | 存在但不可靠 | `ToolResult` 支持 `False/True/None`，`ExternalAgentFailure` 能保留三态；但 `InvocationResult` 当前是布尔字段，适配时会把缺失值压成 `False`。ToolRuntime 外层 timeout/exception 也默认返回 `False`。 |
| `side_effect_state` | 局部存在 | `ToolResult.state` 可以携带，`ExternalAgentFailure` 会读取或推导；但 `InvocationResult`、Runtime 适配字典、`ExecutionResult` 和 `STEP_FAILED` 不保留它。没有操作级状态，组合工具的局部状态不能代表整体状态。 |
| `tool_call_id` | 局部存在 | raw handler、MCP `ToolContext` 和部分 Java 请求头使用它；它不进入 ToolRuntime context、ledger、InvocationResult、ExecutionResult 或事件，因此事后无法稳定按调用关联证据。 |
| `execution_id` | 上层存在 | `ToolInvocationContext`、账本、ExecutionEvent、TraceEvent 和 RuntimeResult 中存在；它没有随着 ToolResult/InvocationResult 一起传递，外部失败事实本身不能独立关联到 execution。 |
| `step_id` | 上层存在 | context、账本、trace 和事件中存在；`ToolResult`、`InvocationResult`、`ExecutionResult` 没有统一携带，失败结果离开调用闭包后不能独立定位步骤。 |

此外，当前还存在以下容易被误认为“已经完整”的字段：

- `trace_id` 在部分 MCP/外部响应和 `ExternalAgentFailure` 中存在，但 Runtime 适配字典没有完整保留；RuntimeResult 的 trace 不能替代每次请求的证据。
- `receipt_id` 在部分 Java 成功响应中存在，但很多失败 helper 不接收或丢弃 receipt；失败时尤其不能假设 receipt 一定为空。
- `idempotency_key` 在 `ToolInvocationContext` 和 MCP/Java 请求路径中存在，但没有统一回传到失败证据；重试时无法仅凭 `ExecutionResult` 证明应该复用哪个逻辑键。
- `side_effect_committed` 是 RuntimeResult 级别的最终聚合信号，主要由 artifact/draft 结果推断，不能替代失败调用的操作级副作用证据。

### 1.4 当前最关键的丢失点

当前不是“没有任何证据”，而是证据在跨层适配时不具备 lossless 语义。最关键的丢失点有四个：

1. `InvocationResult.from_tool_result()` 只读取有限字段，并把缺失的 `request_sent` 转成 `False`。
2. `RuntimeAgentService.invoke_fn()` 将 InvocationResult 压缩成有限字典，丢弃 `state`、receipt、resource refs、调用身份和更多上下文。
3. ToolRuntime 自身 timeout 或 handler exception 只知道“调用等待失败”，不知道下游请求是否已经送达。
4. `content.create_draft` 是 Creator → poll → artifact → Java POST → Java verify 的组合操作。某个叶子调用的 `request_sent=False` 不能证明整个操作无副作用；但当前组合工具没有持续维护操作级证据。

因此，当前 `ExternalAgentFailure` 可以统一“已观察到的失败事实”，但还不能保证这个事实包含了 Retry 所需的完整执行证据。

---

## 2. Retry 所需 Evidence 设计

未来 Retry Engine 的输入不应只是 `error_code` 和 `retryable`。它需要一个与失败事实关联的、只追加的执行证据快照。该快照用于回答：

1. 哪一个逻辑操作、哪一次尝试、哪一个下游请求失败？
2. 请求是否已经离开 Runtime，是否被下游接收或处理？
3. 是否已经产生、可能产生或确认产生副作用？
4. 重试是否能复用稳定的幂等身份？
5. 下游返回了什么，失败发生在哪个阶段？

建议未来形成如下逻辑结构。这里是设计形状，不是本阶段的代码接口承诺。

### 2.1 Request Evidence

```json
{
  "operation_id": "logical-operation-id",
  "invocation_id": "attempt-id",
  "execution_id": "execution-id",
  "step_id": "step-id",
  "tool_name": "content.create_draft",
  "capability": "content.create_draft",
  "tool_call_id": "transport-call-id",
  "request_hash": "sha256-of-canonical-redacted-request",
  "request_time": "2026-08-10T00:00:00Z",
  "deadline": "2026-08-10T00:00:30Z",
  "request_sent": null
}
```

字段作用：

| 字段 | 作用 |
| --- | --- |
| `operation_id` | 标识一次逻辑业务操作，在同一逻辑操作的多次 attempt 间保持稳定；组合工具的所有子调用都应能归属到它。 |
| `invocation_id` | 标识一次 Runtime 尝试；它可以每次不同，不能单独作为重试幂等身份。 |
| `execution_id` / `step_id` | 将证据关联到 Execution 和具体步骤。 |
| `tool_name` / `capability` | 确定契约、外部系统和副作用类型。 |
| `tool_call_id` | 关联一次 MCP/传输调用及下游请求日志；不应代替稳定的业务幂等键。 |
| `request_hash` | 证明尝试的输入是否相同，同时避免在事件中保存原始敏感参数。应基于规范化、去敏后的参数生成，并明确字段排序和版本。 |
| `request_time` | 确定请求尝试和超时发生的时间关系。 |
| `deadline` | 判断重试是否仍在执行期限内。 |
| `request_sent` | 表示已确认未发送、已确认发送或无法判断；`null` 不能默认改成 `False`。 |

需要区分四类身份：

- `operation_id` 是稳定的逻辑操作身份。
- `invocation_id` 是一次尝试身份。
- `tool_call_id` 是一次工具/传输调用身份。
- `idempotency_key` 是发送给外部服务的稳定去重身份，是否与 `operation_id` 一一对应取决于契约，但必须能追溯其来源。

### 2.2 Side Effect Evidence

```json
{
  "side_effect_state": "UNKNOWN",
  "phase": "DOWNSTREAM_WRITE_READ_TIMEOUT",
  "operation_id": "logical-operation-id",
  "external_operation_id": "creator-task-or-java-operation-id",
  "receipt": "receipt-or-null",
  "external_reference": "created-resource-or-null",
  "side_effect_started": null,
  "completed_suboperations": [
    {"name": "creator.create_task", "state": "CONFIRMED"},
    {"name": "java.create_draft", "state": "UNKNOWN"}
  ],
  "evidence_source": "java_response_or_timeout",
  "observed_at": "2026-08-10T00:00:05Z"
}
```

字段作用：

- `side_effect_state` 表达当前可证明的状态，而不是根据错误码猜测。
- `phase` 说明失败发生在 pre-execution、transport、downstream processing、response read、post-validation 还是 reconciliation 阶段。
- `external_operation_id` 用于查询外部任务或操作状态；它可以与 Runtime 的 `operation_id` 不同。
- `receipt` 和 `external_reference` 用于证明提交、定位资源或执行后查询。
- `side_effect_started` 只能作为底层事实，不应单独替代最终状态。
- `completed_suboperations` 对组合工具必需，避免用最后一个子请求的状态覆盖之前已经完成的写入。
- `evidence_source` 和 `observed_at` 使后续判断知道证据来自响应、客户端、服务端账本还是推导。

组合操作必须保存 operation-level summary。例如 Creator 已经返回 task ID，随后轮询或 Java 写入失败时，整个操作不能被标为 `NOT_STARTED`。叶子调用的证据应保留，同时维护逻辑操作的聚合状态。

### 2.3 Idempotency Evidence

```json
{
  "idempotency_key": "stable-logical-key",
  "idempotency_scope": "tenant-or-conversation-scope",
  "retry_safe": false,
  "tool_contract_idempotent": true,
  "key_reused_on_retry": false,
  "key_provenance": "ToolInvocationContext",
  "expires_at": "2026-08-11T00:00:00Z"
}
```

字段作用：

- `idempotency_key` 和作用域决定外部服务能否把重试识别为同一逻辑请求。
- `tool_contract_idempotent` 是静态能力声明，不等于本次请求已经安全。
- `retry_safe` 是基于本次 evidence、契约、操作类型和策略计算出的结论，不能只复制 ToolContract 的 `idempotent`。
- `key_reused_on_retry` 记录重试是否确实使用同一个逻辑键，防止“声明幂等但每次生成新键”。
- `key_provenance` 记录键由谁生成、基于哪些稳定输入生成。
- `expires_at` 用于判断外部服务的幂等记录是否仍然有效；超过 TTL 后即使键相同，也不能盲目认为安全。

当前 `ToolInvocationContext.build()` 的幂等键基于任务、执行、步骤和工具名，并不包含工具参数；MCP 又可能根据业务 scope 生成另一层键。因此未来需要明确 operation-level 与 downstream-level 的映射，不能把任意一个键直接当作完整的幂等证据。

### 2.4 Response Evidence

```json
{
  "status_code": 504,
  "error_code": "RESULT_UNKNOWN",
  "response_payload_hash": "sha256-or-null",
  "response_payload_redacted": null,
  "response_headers": {
    "x-trace-id": "trace-id",
    "x-receipt-id": "receipt-id"
  },
  "response_time": "2026-08-10T00:00:05Z",
  "timeout_phase": "READ",
  "exception_type": "ReadTimeout",
  "transport_outcome": "RESPONSE_UNKNOWN"
}
```

字段作用：

- `status_code` 区分请求未到达、服务端拒绝、服务端已接受但响应错误等情况。
- `error_code` 保留下游原始错误码；包装层应同时保留 `root_error_code`，避免只剩通用 `INTERNAL_ERROR`。
- `response_payload_hash` 和去敏后的摘要支持审计和去重，不要求在事件中保存秘密或完整响应正文。
- `response_headers` 只保留与追踪、receipt、幂等和操作状态相关的白名单字段。
- `response_time` 用于关联发送、处理和读取超时。
- `timeout_phase` 区分 connect、write、read、poll、Runtime deadline 等阶段；“超时”本身不说明副作用状态。
- `exception_type` 和 `transport_outcome` 说明客户端观察到的是未连接、部分写入、响应未知还是响应已收到。

### 2.5 最小证据门槛

Retry Engine 未来至少要能获得以下最小集合：

```text
逻辑操作身份 + attempt 身份
请求送达三态
副作用状态
幂等键及其有效性
错误码与失败阶段
必要的外部操作 ID / receipt / resource reference
```

如果其中任一项对写操作缺失，策略层应选择“不自动重试”，而不是从 `retryable=True` 推断安全。完整 HTTP 或原始工具参数不一定需要进入 ExecutionEvent，但至少应有可审计的摘要、哈希或持久外部引用。

---

## 3. 当前 ToolRuntime 是否满足要求

### 3.1 当前行为审计

| 所需证据 | 当前 ToolRuntime 状态 | 风险 |
| --- | --- | --- |
| 请求是否发送 | `InvocationResult` 有 `request_sent`，但由原始字典转换；缺失值被 `bool(...)` 转为 `False`。ToolRuntime 自己的 timeout 和异常返回也默认 `False`。 | “无法判断”会被误认为“未发送”，对写操作会产生错误的安全信号。 |
| 请求参数摘要 | `ToolInvocationContext` 持有原始 `tool_args`，账本不保存参数或规范化哈希。 | 不能证明两次尝试是否是同一请求，也可能迫使后续组件传递原始敏感参数。 |
| `tool_call_id` | 由 RuntimeAgentService 的 raw handler 生成并传给 MCP；ToolRuntime context 和账本没有此字段。 | 无法从 Runtime 调用记录直接关联 MCP、Java 日志和外部操作。 |
| 下游响应 | 正常原始结果会部分进入账本；失败分支、timeout 分支和 `InvocationResult` 只保留有限字段。 | status、receipt、resource reference、state 和响应头可能在适配时丢失。 |
| timeout 位置 | ToolRuntime 记录耗时并能识别自己的 `asyncio.wait_for` timeout；Java/Creator 可能另有 timeout code。 | 没有结构化的 connect/write/read/poll/Runtime deadline 阶段，无法判断请求是否已部分发送。 |
| 异常位置 | ToolRuntime 记录通用 `TOOL_EXECUTION_FAILED` 和异常字符串；MCP handler 异常也可能被转为同一类错误。 | 异常发生在调用前、发送中、下游处理后还是响应读取时不清楚。 |
| 账本生命周期 | `ToolExecutionLedger` 保存调用状态、时间、错误和部分结果。 | 账本是 Runtime 内存对象，且 RuntimeAgentService 在一次 `_execute_single` 中创建；跨进程、重启和未来重试不能依赖它作为最终证据。 |

### 3.2 当前适配链的证据压缩

当前最明显的压缩发生在两处：

1. `InvocationResult.from_tool_result()` 只复制成功/失败、数据、错误码、消息、`retryable` 和布尔化的 `request_sent`，没有复制 `state`、`trace_id`、`receipt_id`、`resource_refs` 或下游响应元数据。
2. `RuntimeAgentService.invoke_fn()` 又把 InvocationResult 映射为有限字典，主要用于 CapabilityExecutor 和最终展示，进一步丢弃调用身份和失败证据。

因此，即使 MCP 当时返回了较完整的 `ToolResult.state`，Worker 接收到的 `ExecutionResult` 也未必能看到它。

### 3.3 最小补充位置（仅设计，不实施）

未来最小补充应沿现有边界补齐，而不是在 Worker 重新猜测：

- 在 `ToolInvocationContext`/账本入口建立请求身份、规范化参数摘要和时间证据。
- 在 `InvocationResult` 增加一个 lossless 的 evidence projection，至少保留三态 `request_sent`、副作用状态、phase、receipt、外部操作 ID 和响应摘要。
- 在 RuntimeAgentService 适配时原样传递 evidence，而不是重新压缩成错误码字典。
- 在组合 MCP tool 返回结果时维护 operation-level evidence，并保留已完成子操作。
- 在后续 External Operation Tracking 阶段把这些证据放入可持久的账本或事件引用中。

这些是后续阶段的设计入口；Phase 10-E-0 不实施其中任何代码改动。

---

## 4. MCP 层证据传递分析

### 4.1 情况 A：请求没有发送

典型路径是输入校验或 handler 签名校验在 MCP pre-execution 阶段失败：

```text
MCP schema/handler validation failure
    ↓
request_sent = False
state.phase = PRE_EXECUTION_VALIDATION_FAILED
state.downstream_called = False
state.side_effect_started = False
```

这一类证据在当前 MCP Server 的特定校验分支中相对明确，通常可归为 `NOT_STARTED`。但这只说明该 MCP handler 没有调用下游；若组合工具在此之前已经完成了其他子操作，不能据此证明整个 operation 没有副作用。

### 4.2 情况 B：请求已经发送并失败

当前有几类可以明确表达“已发送”的路径：

- handler 输出校验失败时，MCP 标记 POST_EXECUTION、`request_sent=True`，并说明下游已调用。
- Java 的部分 4xx、业务拒绝、资源冲突和已收到的响应会返回 `request_sent=True`。
- 外部服务返回成功后，后续本地校验失败也可能已经有真实副作用。

但该语义目前不一致：

- MCP handler 异常统一返回 `request_sent=False`，即使 handler 可能已经发出下游请求。
- Java 5xx 或依赖不可用的 helper 常使用 `request_sent=False`，但服务端可能已经接收并处理写请求。
- 失败 helper 往往不保留响应 status、receipt 和服务端返回的操作引用。

因此当前 `True` 能说明“至少请求已发送/下游已被调用”，但当前 `False` 不能反向证明“绝无副作用”。

### 4.3 情况 C：请求结果未知

当前 Java client 对写操作的 read timeout 可以返回 `RESULT_UNKNOWN`，并设置 `request_sent=True`；但没有统一填充：

```text
side_effect_state = UNKNOWN
phase = WRITE_RESPONSE_READ_TIMEOUT
transport_outcome = RESPONSE_UNKNOWN
```

由于 `ExternalAgentFailure` 在没有显式 `side_effect_state` 时会从 `request_sent=True` 推导为 `POSSIBLE`，这种结果的“副作用未知”语义可能退化成“可能发生”。分类器可以通过 `RESULT_UNKNOWN` 识别风险，但证据层仍缺少明确的未知状态和失败阶段。

更严重的是，ToolRuntime 自己的等待超时或 handler 异常会返回默认 `request_sent=False`，然后可能被归为 `NOT_STARTED`。这无法区分：

```text
连接前失败
    vs
写入后未读到响应
    vs
下游已处理但 MCP handler 未能返回
```

### 4.4 MCP 未来应传递的统一证据

后续 MCP/ToolResult 契约应能够在每个失败路径传递以下状态摘要：

```json
{
  "request_sent": null,
  "state": {
    "phase": "DOWNSTREAM_WRITE_READ_TIMEOUT",
    "side_effect_state": "UNKNOWN",
    "side_effect_started": null,
    "downstream_called": true,
    "operation_id": "logical-operation-id",
    "external_operation_id": "external-id-or-null",
    "response_status": null,
    "timeout_phase": "READ",
    "transport_outcome": "RESPONSE_UNKNOWN",
    "evidence_source": "java_client"
  },
  "trace_id": "trace-id",
  "receipt_id": null
}
```

传递规则：

- 能证明未发送时才使用 `False`。
- 能证明请求已发送或下游已调用时使用 `True`，并继续独立表达副作用状态。
- 无法判断时使用 `None`，副作用状态使用 `UNKNOWN`，不能为了兼容旧 helper 填 `False`。
- `request_sent` 和 `side_effect_state` 是两个维度；`True` 不等于 `CONFIRMED`，`False` 也不自动覆盖组合操作的历史子操作。
- 所有失败路径都应保留操作身份和阶段，不能只在成功响应中提供 receipt 或 resource reference。

---

## 5. Java / Creator 外部服务需要提供的信息

本节只设计 Runtime 需要的外部契约，不修改 Java Backend 或 Creator Agent。

### 5.1 Java 写操作建议响应

对于创建草稿、发布等外部写操作，单纯的 `success/failure` 不足以支持安全重试。建议外部服务最终提供可查询、可去重的操作响应：

```json
{
  "operation_id": "java-operation-id",
  "operation_status": "ACCEPTED",
  "request_received": true,
  "request_processed": true,
  "status": "SUCCEEDED",
  "created_resource_id": "draft-id",
  "receipt": "receipt-id",
  "idempotency_key": "stable-key",
  "server_time": "2026-08-10T00:00:05Z",
  "trace_id": "trace-id",
  "error": null
}
```

失败或超时也应返回/可查询：

- 服务端是否接收到请求；
- 是否开始处理；
- 是否已经提交事务；
- 外部操作 ID、receipt 或资源 ID；
- 服务端记录的幂等键及其过期时间；
- 服务端状态查询入口和最后观察时间；
- 原始错误码、HTTP 状态、处理阶段和 trace。

其中 `ACCEPTED`、`PROCESSING`、`SUCCEEDED`、`REJECTED`、`FAILED`、`UNKNOWN` 等状态需要与“请求已发送”分开。一个 HTTP 5xx 只说明客户端收到 5xx，不足以说明写操作没有执行。

### 5.2 Creator 操作建议响应

Creator 已经有 task、poll 和 artifact 这类异步操作概念。Runtime 至少需要能够取得：

```json
{
  "operation_id": "creator-task-id",
  "task_id": "creator-task-id",
  "status": "PROCESSING",
  "request_received": true,
  "request_processed": true,
  "artifact_id": null,
  "receipt": null,
  "idempotency_key": "stable-key",
  "created_at": "2026-08-10T00:00:00Z",
  "last_observed_at": "2026-08-10T00:00:05Z",
  "last_error": null
}
```

至少应满足：

- `create_task` 返回的 task ID 可作为外部操作 ID；
- poll 超时能说明“任务仍可能存在”，而不是只返回普通 `TIMEOUT`；
- artifact ID、handoff ID 或其他资源引用在失败路径也能被保留；
- 非 2xx 响应保留状态码、阶段和最后一次服务端快照；
- 同一幂等键能查询既有任务，避免 Runtime 以新任务替代未知旧任务。

### 5.3 为什么这些信息是 Runtime 必需的

Runtime 需要决定的是“能否安全采取下一动作”，不是简单地把某个异常转换成用户消息。没有外部操作 ID、receipt 或服务端状态查询能力时，Runtime 无法区分：

- 从未创建任务；
- 已创建但还在处理；
- 已创建资源但响应丢失；
- 服务端拒绝且无副作用；
- 服务端状态本身未知。

因此外部服务的可查询操作状态是未来 Reconciliation 的前提，也是 Retry Engine 能否安全运行的前提。ToolContract 中的 `idempotent`、`max_attempts` 和错误码白名单只能作为静态策略输入，不能替代这些动态证据。

---

## 6. Retry 安全矩阵设计

下表是未来策略的安全基线。它描述“证据状态下可以考虑什么”，不是当前阶段执行 Retry 的实现。

| `request_sent` | `side_effect_state` | 对外部写操作的默认结论 | 后续动作 |
| --- | --- | --- | --- |
| `False` | `NONE` | 可证明没有副作用 | 仅当错误属于瞬态、契约允许、幂等/操作类型安全、预算和 deadline 允许时，才可作为自动 Retry 候选；否则 FAIL_FAST。 |
| `False` | `NOT_STARTED` | 请求未开始且没有副作用 | 同上；应记录 evidence，而不是把 `False` 当作永久事实。 |
| `False` | `POSSIBLE` | 请求字段与副作用证据矛盾，仍可能已有子操作 | 禁止盲目 Retry；进入 Reconciliation，无法查询时人工介入。 |
| `False` | `UNKNOWN` | 送达和副作用都无法确定 | 禁止自动 Retry；需要 Reconciliation 或人工介入。 |
| `False` | `CONFIRMED` | 已确认存在副作用 | 禁止重放同一逻辑写操作；按资源/操作状态继续处理或人工介入。 |
| `True` | `NONE` | 请求已送达，但尚无副作用证据 | 对纯读、明确无副作用的调用可由策略单独评估；对写操作默认不自动重试，除非有权威账本证明未处理且幂等保护有效。 |
| `True` | `NOT_STARTED` | 请求已送达但服务端报告未开始 | 只有权威服务端证据和可验证幂等契约同时存在时才可成为 Retry 候选；一般先保守处理。 |
| `True` | `POSSIBLE` | 已送达，可能已有副作用 | 禁止自动 Retry；必须 Reconciliation。 |
| `True` | `UNKNOWN` | 已送达，最终结果未知 | 禁止自动 Retry；必须查询外部状态，无法查询时人工介入。 |
| `True` | `CONFIRMED` | 已确认存在副作用 | 禁止重放同一逻辑写操作；使用资源/receipt 继续后续流程或人工处理。 |
| `None` | `NONE` | Runtime 无法证明送达，但当前没有已知副作用 | 对写操作默认不能自动 Retry；只有权威证据明确“未接收/未处理”时才可重新评估。 |
| `None` | `NOT_STARTED` | 推断未开始，但请求送达未知 | 不能仅凭推断自动 Retry；先获取权威送达证据。 |
| `None` | `POSSIBLE` | 送达未知且可能有副作用 | 禁止自动 Retry；必须 Reconciliation。 |
| `None` | `UNKNOWN` | 送达和结果均未知 | 禁止自动 Retry；必须 Reconciliation 或人工介入。 |
| `None` | `CONFIRMED` | 已确认有副作用 | 禁止重放同一逻辑写操作。 |

矩阵的补充规则：

1. `INVALID_ARGUMENT`、`AUTH_FAILURE`、`PERMISSION_DENIED`、`CONTRACT_MISMATCH`、确定性的业务状态错误默认 FAIL_FAST 或请求用户补充信息，不因 `retryable=True` 自动重试。
2. `DEPENDENCY_UNAVAILABLE`、TIMEOUT、NETWORK_ERROR、RATE_LIMIT 只有在证据和预算允许时才是 Retry 候选；错误类别本身不授予 Retry 权限。
3. `RESULT_UNKNOWN`、写响应读取超时、handler 在下游调用后抛异常等情况应按未知/可能副作用处理，而不是按普通网络错误处理。
4. 组合工具以 operation-level evidence 为准。某个子调用的 `request_sent=False` 不能清除此前已完成的 Creator 或 Java 子操作。
5. `CONFIRMED` 不是“失败可以重试”，而是“已发生，需要继续观察、去重或补偿”。

---

## 7. Execution Event 中未来应该记录什么

### 7.1 当前状态

Phase 10-D 的 `STEP_FAILED` 已经记录：

```text
failure_category
recovery_action
recovery_reason
error_code
error_message
retryable
```

这足以表达一次决策结果的最小摘要，但不足以审计“为什么这个决策安全”，也不足以让后续 Worker、Retry Engine 或 Reconciliation 重建失败时的外部事实。

### 7.2 未来事件形状

未来可以保持现有 `STEP_FAILED` 事件类型不变，扩展其 payload，或增加版本化 evidence 事件。建议至少具有以下逻辑结构：

```json
{
  "step_execution_id": "step-attempt-id",
  "attempt": 1,
  "retry_count": 0,
  "failure": {
    "category": "DEPENDENCY_UNAVAILABLE",
    "raw_error_code": "JAVA_BACKEND_UNAVAILABLE",
    "source": "java",
    "dependency": "java",
    "phase": "DOWNSTREAM_CONNECT",
    "request_sent": null,
    "side_effect_state": "UNKNOWN",
    "execution_id": "execution-id",
    "step_id": "step-id",
    "invocation_id": "invocation-id",
    "tool_call_id": "tool-call-id",
    "operation_id": "logical-operation-id",
    "trace_id": "trace-id",
    "receipt_id": null,
    "idempotency_key": "stable-key",
    "request_hash": "request-hash",
    "evidence_version": "1",
    "evidence_source": "mcp/java-client",
    "observed_at": "2026-08-10T00:00:05Z",
    "response": {
      "status_code": null,
      "timeout_phase": "CONNECT",
      "transport_outcome": "NOT_CONNECTED"
    }
  },
  "decision": {
    "action": "FAIL_FAST",
    "retry_allowed": false,
    "wait_allowed": false,
    "reconciliation_required": true,
    "human_required": false,
    "reason": "送达和副作用证据不足，写操作禁止盲目重放",
    "policy_version": "phase-10-e"
  }
}
```

### 7.3 事件设计原则

- 事件应是不可变的事实快照；后续新的查询结果应追加新事件，而不是覆盖原失败事件。
- 失败分类和恢复决策要与 evidence 同时记录，避免只保存最终动作而丢失决策依据。
- 原始工具参数和响应正文可能包含用户内容、认证信息或秘密；事件只保存规范化哈希、去敏摘要和外部证据引用。
- `request_sent`、`side_effect_state`、`operation_id`、`idempotency_key` 和 evidence version 应能被后续组件直接读取，不要依赖解析错误消息。
- `retry_count` 表示已发生的尝试次数，不等于允许的下一次尝试；是否允许由 `RecoveryDecision` 和后续 Retry Engine 计算。
- 事件中的 `reconciliation_required=true` 不意味着当前要进入新状态；在本阶段它只是未来行动的事实记录/决策结果。

### 7.4 与当前 Execution 状态的关系

本审计不建议现在增加 `WAITING_DEPENDENCY`、`WAITING_RETRY` 或 `UNKNOWN_RESULT` 状态。当前仍可用已有 FAILED/PAUSED 语义承载 Phase 10-D 的最小决策，但在事件中记录结构化 evidence，避免用状态枚举掩盖证据不足。

未来是否增加状态，应在 Retry、Dependency Waiting 和 Reconciliation 的生命周期语义确定后统一设计；仅为保存证据而增加状态会把“事实”“决策”和“调度生命周期”混在一起。

---

## 8. 后续实施路线

### Phase 10-E-0：Execution Evidence Audit（当前阶段）

- 完成现状盘点、丢失点和安全矩阵。
- 定义最小 evidence envelope、身份关系和事件记录要求。
- 设立 Retry Engine 的准入门槛：缺失或不确定的写操作证据不得自动重放。

### Phase 10-E：Retry Engine

- 消费结构化 `RecoveryDecision` 和 evidence，而不是直接读取 `ExecutionResult.error_code`。
- 实现有界 attempt、预算、deadline、backoff 和取消语义。
- 对安全重试复用同一逻辑 operation/idempotency key，并将每次尝试写入事件。
- 明确禁止对 POSSIBLE、UNKNOWN 或证据缺失的写操作盲目重试。

### Phase 10-F：Dependency Waiting

- 在依赖可用性、等待期限、唤醒条件和失败升级规则确定后，再设计等待调度。
- 将等待原因与原始 evidence、RecoveryDecision 关联。
- 本阶段不提前新增 `WAITING_DEPENDENCY`。

### Phase 10-G：Reconciliation

- 根据 operation ID、receipt、幂等键、资源引用和外部状态查询确认最终结果。
- 处理超时、响应丢失、部分完成和组合操作的收敛。
- 定义无法查询或冲突时的人工处理边界。

### Phase 10-H：External Operation Tracking

- 建立跨 Runtime 重启和跨进程可查询的外部操作账本。
- 持久化 operation、attempt、子操作、外部引用、状态变迁和证据来源。
- 让 Retry、Waiting、Reconciliation 共用同一操作身份，而不是各自维护临时判断。

### 8.1 Phase 10-E 的进入条件

在开始实现 Retry Engine 前，至少需要确认：

1. `request_sent=None` 不会在任何适配层被转换为 `False`。
2. `side_effect_state` 能从外部结果传递到 Worker-facing decision boundary。
3. ToolRuntime timeout、handler exception 和下游 timeout 能区分失败阶段，或明确标记为 UNKNOWN。
4. 组合操作有稳定的 operation-level evidence，不会被最后一个叶子调用覆盖。
5. 幂等键、外部 operation ID、receipt/resource reference 能在失败路径保留或查询。
6. STEP_FAILED 或等价事件能够保存决策所依据的 evidence 摘要。
7. 缺少上述证据时，策略默认 FAIL_FAST、REQUEST_USER_INPUT、REQUIRE_RECONCILIATION 或 MANUAL_INTERVENTION，而不是自动 Retry。

---

## 9. 审计结论

当前 Runtime 已经完成“失败事实规范化”和“Worker 消费恢复决策”的第一步，但还没有完成“可安全重试所需的执行证据闭环”。当前最主要的工程问题不是缺少更多错误码，而是：

```text
局部 ToolResult 有证据
    ↓ 适配时压缩
Worker 只拿到部分失败事实
    ↓
无法证明写请求是否送达、是否产生副作用、是否可以复用幂等身份
```

因此下一步应先补齐 lossless evidence 传递和操作级追踪设计，再进入 Retry Engine。任何将 `JAVA_BACKEND_UNAVAILABLE` 或其他依赖错误直接映射为“安全重试”的实现，都不符合本审计定义的通用安全边界。

本阶段仅新增本设计文档；没有修改 Worker、RuntimeAgentService、Planner、ToolRuntime、MCP ToolContract、Creator Agent、Java Backend 或 ExecutionStateManager schema，也没有实现任何恢复动作。
