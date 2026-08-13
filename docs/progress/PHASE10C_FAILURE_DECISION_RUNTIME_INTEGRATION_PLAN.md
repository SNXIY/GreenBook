# Phase 10-C：Failure Decision Runtime Integration 设计分析

## 0. 范围、前提与结论

本阶段只做 Runtime Failure Decision 的接入设计和当前代码状态核对。

本阶段不：

- 修改 `Worker`、`RuntimeAgentService`、`Planner`、`ToolRuntime`、`ExecutionStateManager`、`ToolContract`、Creator Agent 或 Execution 状态模型；
- 实现 `Retry`、`WAITING_DEPENDENCY`、`Reconciliation` 或自动恢复；
- 启动服务、调用 Creator/Java/MCP，或运行完整测试；
- 为 `JAVA_BACKEND_UNAVAILABLE` 增加特殊分支。

核心结论：

> Phase 9-B 的 `ExternalAgentFailure` 已经存在于 contracts 层，但尚未接入真实 Runtime 执行链。当前 Worker 仍按原始 `error_code` 调用旧的 `RecoveryPolicy`，因此缺少“分类 → 策略 → 决策”的运行时决策边界。

本阶段选择的未来接入位置是：

```text
CapabilityExecutor 返回失败结果
    ↓
Worker-facing Failure Decision Boundary
    ├─ FailureNormalizer / lossless failure adapter
    ├─ FailureClassifier
    └─ FailurePolicy
    ↓
RecoveryDecision
    ↓
Worker 消费决策
```

这里的“接入位置”是 Worker 的 step failure decision boundary，而不是把分类规则
写进 Worker。未来应由一个独立的决策组件承载纯函数和策略规则，Worker 只组装
上下文、消费 `RecoveryDecision` 并负责 Execution 生命周期。

另一个必须先满足的前提是：失败事实必须在 `CapabilityExecutor` 将原始结果压缩为
`ExecutionResult` 之前被无损保留，至少包括原始 `error_code`、`request_sent` 三态、
`side_effect_state`、`phase`、`receipt_id`、`trace_id`、`idempotency_key` 和
下游 metadata。否则 Worker 即使调用了 classifier，也只能对不完整证据做决策。

---

## 1. 当前失败链路分析

### 1.1 Phase 9-B 契约链与真实 Runtime 链的区别

Phase 9-B 定义的 contracts 链是：

```text
ToolResult
    ↓ normalize_external_failure(...)
ExternalAgentFailure
```

但是对当前仓库进行代码扫描后，没有发现 `FailureNormalizer` 或
`normalize_external_failure` 被 `RuntimeAgentService`、`ExecutionWorker`、
`CapabilityExecutor` 或 `ToolRuntime` 调用。它目前只存在于 contracts 模块、导出入口和
单元测试中。

因此不能把下面这条链路描述为“当前已经生效”：

```text
Tool Failure → ExternalAgentFailure → Execution FAILED
```

这条链路是 Phase 9-B 规定的未来接入语义。当前真实 Runtime 是：

```text
Tool/MCP/下游失败
    ↓
InvocationResult / ExecutionResult
    ↓
ExecutionWorker 按原始 error_code 判断
    ↓
StepExecution FAILED 或 FAILED_RETRYABLE
    ↓
Execution FAILED
```

`ExternalAgentFailure` 当前是可供未来接入的事实模型，不是当前 Execution 状态中
已经保存或消费的对象。

### 1.2 真实执行路径

对“明天上午八点发布一篇关于如何学好 Java 的帖子”这一类已经进入新 Runtime 的
请求，当前源码确定的关键路径如下：

```text
ConversationRuntimeAdapter
    → RuntimeAgentService._execute_single
    → ExecutionWorker._execute_one_step
    → CapabilityExecutor.execute_step
    → ToolRuntime.invoke
    → MCPServer.execute_tool("content.create_draft")
    → content.create_draft
       ├─ CreatorClient.create_task / wait / get_artifact
       ├─ JavaClient.create_draft
       └─ JavaClient.get_draft
```

关键位置和当前职责：

| 阶段 | 代码位置 | 当前事实 |
|---|---|---|
| 下游错误产生 | `packages/java_client/greenbook_java_client/client.py::_request` | Java 连接失败、连接超时、网络错误或 5xx 等情况被映射为 `ToolResult`；写入后读超时可映射为 `RESULT_UNKNOWN`。 |
| MCP 业务传播 | `services/greenbook_mcp/greenbook_mcp_server/tools/content.py::create_draft` | Creator 成功后调用 Java；Java 的失败 `ToolResult` 被直接返回，不创建 `ExternalAgentFailure`。 |
| MCP 边界 | `services/greenbook_mcp/greenbook_mcp_server/server.py::execute_tool` | 对输入、handler signature、输出 envelope 做契约校验，并返回 dict。 |
| ToolRuntime | `packages/assistant_core/.../execution/runtime/tool_runtime.py::invoke` | 记录本次 tool invocation、ledger 状态和 trace；将 raw dict 转为 `InvocationResult`。 |
| Runtime 适配 | `apps/assistant_api/.../services/runtime_agent_service.py::invoke_fn` | 将 `InvocationResult` 再压成给 `CapabilityExecutor` 的 dict。 |
| CapabilityExecutor | `packages/assistant_core/.../execution/capability_executor.py::execute_step` | 将失败 dict 转成 `ExecutionResult.from_tool_error(...)`。 |
| Worker 决策 | `packages/assistant_core/.../execution/worker.py::_execute_one_step` | 在 `result.approval_required` 之后调用 `RecoveryPolicy.can_retry_failure(step_ex, result.error_code)`。 |
| Execution 状态 | `packages/assistant_core/.../execution/state_manager.py` | 永久失败调用 `fail_step(..., permanent=True)`，随后更新 Execution 状态并跳过下游步骤。 |
| Runtime 输出 | `apps/assistant_api/.../services/runtime_agent_service.py::_finish_execution` | 从失败 Step 读取 `error_code`/`error_message`，生成 `RuntimeResult(status="FAILED")`。 |

### 1.3 `JAVA_BACKEND_UNAVAILABLE` 的当前传播细节

`JavaClient._request` 对不同传输事实有不同的原始映射：

- `ConnectError`、连接超时、pool 超时和部分网络错误：`JAVA_BACKEND_UNAVAILABLE`；
- 写请求的 `ReadTimeout`：`RESULT_UNKNOWN`，`request_sent=True`；
- 读请求的 `ReadTimeout`：`TIMEOUT`；
- `WriteTimeout`：`REQUEST_NOT_SENT`，`request_sent=False`；
- HTTP 5xx：当前也可能映射为 `JAVA_BACKEND_UNAVAILABLE`。

问题在于，HTTP 5xx 的 `ToolResult.java_backend_unavailable()` 当前默认
`request_sent=False`，与“请求已到达并由 Java 返回 5xx”的事实并不等价。这个事实
差异必须在后续接入前被保留或明确修正；Phase 10-C 不在代码中修复它。

### 1.4 当前失败信息在哪里丢失

当前链路并不是完整的失败事实传递链，存在以下压缩点：

1. `ToolRuntime.InvocationResult.from_tool_result()` 只复制 `ok`、`data`、`code`、
   message、`retryable` 和 `request_sent` 等有限字段；`state`、`receipt_id` 等不能
   完整进入 `InvocationResult`。
2. `RuntimeAgentService.invoke_fn()` 返回给 `CapabilityExecutor` 的 dict 也只包含
   有限字段，不包含完整 `state`、回执和下游 metadata。
3. `CapabilityExecutor` 使用 `bool(result.get("request_sent", False))`，会把
   `request_sent=None` 压缩成 `False`；这会把“无法确认”错误地表现成“确认未发送”。
4. `ExecutionResult` 目前只有布尔 `request_sent`，没有 `side_effect_state`、
   `phase`、`receipt_id` 或原始 failure metadata。
5. Worker 的 `STEP_FAILED` payload 当前只有 `retryable`、`error_code` 和
   `error_message`，没有分类、策略、side-effect 风险或决策理由。

因此，未来接入不能在 Worker 中从 `ExecutionResult.error_code` 重新猜测
`ExternalAgentFailure`。必须先定义一个不丢失失败证据的适配边界；这个适配边界可以
是 transient failure envelope，也可以是由上游直接传递的已规范化事实，但不能依赖
重新猜测。

### 1.5 当前为什么直接得到 `Execution FAILED`

当前 `RecoveryPolicy.DEFAULT_RETRYABLE_CODES` 只有：

```text
TIMEOUT
NETWORK_ERROR
RATE_LIMIT
TEMPORARY_UNAVAILABLE
```

其中没有 `JAVA_BACKEND_UNAVAILABLE`。虽然 ToolContract 中
`content.create_draft` 的 `RetryPolicy` 已声明部分 transient error 和
`max_attempts=2`，当前 Worker 并没有读取该 ToolContract policy，而是使用自己的
error-code 白名单。

所以当前路径是：

```text
JAVA_BACKEND_UNAVAILABLE
    → RecoveryPolicy.can_retry_failure = False
    → STEP_FAILED(retryable=false)
    → fail_step(permanent=True)
    → 下游步骤 SKIPPED
    → ExecutionStatus.FAILED
```

现有 `FAILED_RETRYABLE`、`RetryManager` 和显式 `retry_step` 是已有的手动/后续
Worker pass 机制，不是 Phase 10-C 的 Failure Decision Runtime，也不能表达依赖
等待、未知写入或对账要求。

### 1.6 当前缺少的决策能力

缺失点不在 Java client 是否能返回错误，而在失败进入 Worker 后没有统一决策层来
同时读取：

- `ExternalAgentFailure` 的原始事实；
- `request_sent` 三态和 `side_effect_state`；
- 当前 Step 的 attempt、retry budget 和 deadline；
- ToolContract 的副作用、幂等和 retry metadata；
- ledger、receipt 和 reconciliation 的已知结果；
- 用户/租户策略以及是否需要用户或人工介入。

没有这一层，Runtime 只能做“按错误码失败”或“按错误码重试”，无法回答：

```text
这是暂时不可用，还是结果未知？
请求是否已经越过外部边界？
是否可以安全重放？
是否应该等待依赖？
是否必须先对账？
```

---

## 2. FailureClassifier 接入位置分析

### 2.1 选择：Worker-facing Failure Decision Boundary

最终选择：在 `ExecutionWorker._execute_one_step` 已拿到失败结果、但尚未调用旧的
`RecoveryPolicy` 之前，接入一个独立的 Failure Decision 组件：

```text
CapabilityExecutor.execute_step(step)
    → lossless failure adapter / FailureNormalizer
    → FailureClassifier
    → FailurePolicy
    → RecoveryDecision
    → Worker 消费
```

这个位置同时满足两个边界要求：

1. **失败事实仍然属于执行结果边界**：Normalizer 必须在原始 ToolResult/state 尚未
   丢失时完成，或由 CapabilityExecutor 携带等价的无损 envelope；不能等到
   `ExecutionResult` 已经压缩后再反推。
2. **最终策略必须属于 Step 生命周期边界**：attempt、`max_retries`、Execution
   deadline、当前 step 状态、下游依赖和后续调度都由 Worker/执行协调层掌握。

因此，如果必须指定“谁调用 `FailureClassifier`”，答案是 **Worker-facing 的
failure decision boundary**；如果必须指定“谁保存原始 ToolResult 证据”，答案是
**CapabilityExecutor 与 ToolRuntime 之间的结果适配边界**。两者不能被混为一个
下游 HTTP client 的特殊分支。

Worker 本身不应内嵌一张巨大错误码表。未来可以将
`FailureDecisionRuntime.decide(...)` 作为独立组件注入 Worker，由它顺序调用
Normalizer、Classifier 和 Policy；Worker 只调用这个组件并消费其数据结果。

### 2.2 候选层比较

| 候选位置 | 适合之处 | 不适合作为最终接入点的原因 | 结论 |
|---|---|---|---|
| `ToolRuntime` | 最接近 MCP handler、transport timeout、invocation ledger 和幂等 key；可以观察 raw result。 | 不拥有 Step attempt、Execution deadline、租户策略和 ToolContract 的完整策略上下文；同一 Runtime 也可能被非 Worker 调用；在这里决定 retry/wait 会把执行策略下沉到通用调用基础设施。 | 可提供原始证据/ledger snapshot，不拥有最终分类策略。 |
| `CapabilityExecutor` | 知道 `PlanStep`、capability、tool name，并且是 raw tool result 被转为 `ExecutionResult` 的最后边界。 | 当前返回模型会丢失 state 和三态 `request_sent`；它不拥有 retry budget、当前 step 生命周期和完整 ToolContract policy；把最终 Policy 放入这里会让“执行一次工具”和“决定整个 Step 如何恢复”耦合。 | 负责或协助无损适配；不单独拥有最终 RecoveryDecision。 |
| `Worker` | 直接拥有 Step 生命周期、attempt/retry_count、`max_retries`、失败分支和状态更新时机；能把决策交给后续 retry/wait/reconciliation engine。 | 若只拿当前 `ExecutionResult`，证据已经不足；因此必须依赖上游无损 failure envelope。 | **选择为决策边界拥有者**，但通过独立组件保持 Worker 薄。 |
| `RuntimeAgentService` | 负责组装一次 Runtime、ToolRuntime、ledger 和 Worker，也能看到最终 RuntimeResult。 | 不拥有单个 Step 的状态转换；Worker 明确拥有每个 step 的生命周期；在这里决定会绕过 Worker，破坏状态单一来源，并且不适合逐步策略。 | 负责依赖注入和组件装配，不调用每个失败的 classifier/policy。 |

### 2.3 为什么不在 Java/Creator/MCP 中直接决定

Java、Creator 和 MCP 只应负责报告它们观察到的事实，例如状态码、异常类型、
请求是否发送、回执和下游 phase。它们不能决定 Runtime 是否重试，因为：

- 同一个下游错误在读操作和写操作上的风险不同；
- 同一个错误在不同 attempt 预算、deadline、租户策略下动作不同；
- 下游 client 不知道上游 Step 是否已执行过其它子操作；
- client 内部决定重试会绕过 Execution event、幂等键和 Worker 状态。

下游只提供事实，Runtime 统一分类和决策。

---

## 3. Failure Decision Flow 设计

### 3.1 总体流程

```text
原始 ToolResult / Invocation failure envelope
    ↓
FailureNormalizer
    ↓
ExternalAgentFailure
    ↓
FailureClassifier.classify(failure)
    ↓
FailureClassification
    ↓  + FailurePolicyContext
FailurePolicy.decide(classification, context)
    ↓
RecoveryDecision
    ↓
Worker / 后续 Retry、Dependency Waiting、Reconciliation、Human Interaction engine
```

Phase 10-C 只定义这条数据流和调用边界，不执行最后一行的动作。

### 3.2 每层职责

#### `ExternalAgentFailure`

它是不可变失败事实，至少保留：

- 原始 `error_code`，不被 `FailureCategory` 覆盖；
- `dependency`、`phase`、原始 message；
- `request_sent: False | True | None`；
- `side_effect_state`；
- `trace_id`、`receipt_id`、`idempotency_key`；
- 原始 state/transport metadata。

它不回答“下一步一定做什么”。Phase 9-B 的 `recovery_action` 只能作为保守提示，
不能直接授权 Worker。

#### `FailureClassifier`

负责回答：**“这个错误是什么？”**

输入一个已经无损规范化的 `ExternalAgentFailure`，输出稳定、可审计的
`FailureClassification`。它应：

- 按原始 code、dependency、phase、transport metadata 和副作用证据确定类别；
- 计算 `side_effect_risk`，但仍保留原始三态证据；
- 给出安全候选标记和候选恢复方向；
- 生成包含证据来源的 rationale；
- 保持确定性、无 I/O、无 sleep、无状态写入。

它不应：

- 发起 retry、backoff、健康检查或 receipt 查询；
- 读取或写入 ExecutionRepository/EventStore/Ledger；
- 修改 Execution/Step 状态；
- 为某个 Java、Creator 或帖子请求增加特例。

建议输出形状：

```text
FailureClassification {
    category: FailureCategory
    raw_error_code: str
    dependency: str
    retryable_candidate: bool
    side_effect_risk: SideEffectState
    request_sent: bool | None
    phase: str | None
    requires_reconciliation: bool
    requires_human: bool
    rationale: str
}
```

其中 `retryable_candidate` 只是分类事实，不是执行许可。

#### `FailurePolicy`

负责回答：**“遇到这个错误，在当前上下文下应该采取什么策略？”**

它读取 `FailureClassification` 和不可变的 `FailurePolicyContext`，应用：

- ToolContract 的 `has_side_effect`、`idempotent`、`RetryPolicy`；
- 当前 attempt、retry budget 和全局 deadline；
- receipt/ledger/reconciliation 的已知快照；
- 用户/租户策略、权限和人工接管规则；
- 限流窗口、backoff 上限和 policy version。

它也应是无外部执行副作用的决策函数：可以读取已经组装好的上下文快照，但不能
在 `decide()` 内自行调用 Java、Creator、MCP 或修改 Execution。

建议上下文：

```text
FailurePolicyContext {
    attempt: int
    retry_budget: int
    execution_deadline: datetime | None
    capability: str
    tool_name: str | None
    has_side_effect: bool
    idempotent: bool
    idempotency_key: str | None
    supports_reconciliation: bool
    ledger_evidence: dict | None
    user_requested_retry: bool
    policy_id: str
    policy_version: str
}
```

#### `RecoveryDecision`

它是本次策略评估的不可执行数据结果，不是状态迁移命令。建议形状：

```json
{
  "category": "DEPENDENCY_UNAVAILABLE",
  "raw_error_code": "JAVA_BACKEND_UNAVAILABLE",
  "action": "RETRY_WITH_BACKOFF",
  "retry_allowed": true,
  "wait_allowed": false,
  "reconciliation_required": false,
  "human_required": false,
  "side_effect_state": "NOT_STARTED",
  "retry_after": null,
  "reason": "The transient dependency failure was observed before the write boundary; the idempotent tool still has budget and deadline.",
  "policy_id": "default-runtime-failure-policy",
  "policy_version": "v1"
}
```

建议同时保留 `dependency`、`request_sent`、`idempotency_key`、`trace_id` 和
`evidence`，方便事件审计和后续对账。

布尔字段是安全门，不是彼此独立的建议：

- `reconciliation_required=true` 时，`retry_allowed` 必须为 `false`；
- `human_required=true` 时，不能自动发起写操作；
- `action=WAIT_DEPENDENCY` 只表示未来允许进入等待协调流程，不表示当前已经
  写入等待状态；
- `action=FAIL_FAST` 不代表丢弃原始失败事实。

### 3.3 分类和策略的顺序

未来实现必须保持以下顺序：

1. 先恢复/生成无损 `ExternalAgentFailure`；
2. 再由 `FailureClassifier` 判断类别和风险；
3. 再由 `FailurePolicy` 读取 attempt、预算、deadline 和 ToolContract；
4. 最后才由 Worker 或专门的后续 engine 消费 `RecoveryDecision`。

不能跳过分类直接让 Worker 根据 `error_code` 重试，也不能把
`ExternalAgentFailure.recovery_action` 直接当成最终动作。

### 3.4 分类优先级与 fail-closed 规则

通用分类器应先保护安全和副作用事实，再处理“可暂时恢复”的类别：

1. 认证、权限和确定性参数/契约错误；
2. 已知的未知结果或可能副作用；
3. 限流、超时、网络和依赖不可用；
4. 资源不存在、业务拒绝、状态冲突；
5. 无法解释的 `UNKNOWN`。

如果 `request_sent=False` 与 `side_effect_state=POSSIBLE/UNKNOWN` 冲突，按更保守
的副作用事实处理；如果 `request_sent=None` 没有其它权威证据，按未知处理。

---

## 4. Worker 未来接入设计

### 4.1 当前与未来链路

当前：

```text
execute step
    ↓
ExecutionResult
    ↓
RecoveryPolicy.can_retry_failure(error_code)
    ├─ retryable code → FAILED_RETRYABLE / 后续显式 retry
    └─ 其它 code → FAILED
```

未来：

```text
execute step
    ↓
lossless failure envelope
    ↓
FailureNormalizer
    ↓
ExternalAgentFailure
    ↓
FailureClassifier
    ↓
FailurePolicy + FailurePolicyContext
    ↓
RecoveryDecision
    ↓
Worker 执行生命周期决策
```

未来 Worker-facing 接入点应位于当前 `if result.approval_required` 之后、旧的
`RecoveryPolicy.can_retry_failure(...)` 分支之前。当前阶段不改这段代码；这里仅定义
替换顺序。

### 4.2 Worker 负责什么

未来 Worker 应负责：

- 将当前 Step、attempt、retry_count、max_retries 和 deadline 组装成 policy context；
- 调用独立 Failure Decision 组件，获得一个 `RecoveryDecision`；
- 按决策把控制权交给 Retry、Dependency Waiting、Reconciliation、User Input 或
  Manual Intervention engine；
- 继续作为 Step/Execution 状态迁移的唯一协调者；
- 在未来事件中记录原始 code、category、action、side-effect 风险和 reason；
- 对 `RecoveryDecision` 与当前状态做一致性检查，拒绝不允许的状态迁移。

Worker 不应在失败分支中重新实现一套错误码分类。

### 4.3 Worker 不应该负责什么

Worker 不应该：

- 为 `JAVA_BACKEND_UNAVAILABLE`、`CREATOR_TIMEOUT` 等具体 code 写业务特例；
- 直接解析 HTTP 状态、transport exception 或下游业务响应；
- 自己决定“请求一定没有发出”；
- 在 `POSSIBLE/UNKNOWN` 下直接重放写操作；
- 在 Worker 内部实现无界 sleep、backoff、健康轮询或 receipt 查询；
- 把 `WAIT_DEPENDENCY`、`REQUIRE_RECONCILIATION` 当作已经完成的状态迁移；
- 在没有结果确认时伪造 `COMPLETED` 或返回成功 Artifact；
- 因 Runtime 失败自动切回 Legacy 执行器；
- 修改 ToolContract、Capability 语义或下游错误事实。

Retry engine、dependency waiter 和 reconciliation coordinator 应分别拥有自己的
接口和持久化/调度语义，Worker 只消费它们返回的生命周期结果。

### 4.4 决策事件的未来设计

Phase 10-C 只设计事件内容，不新增事件类型。后续实现应考虑让 step failure event
同时保存：

```text
failure: {
    category,
    raw_error_code,
    request_sent,
    side_effect_state,
    trace_id,
    receipt_id,
    idempotency_key
}
decision: {
    action,
    retry_allowed,
    wait_allowed,
    reconciliation_required,
    human_required,
    reason,
    policy_version
}
```

在这之前，当前 EventStore 和 Execution 状态保持不变。

---

## 5. Execution 状态机影响分析

### 5.1 当前状态模型事实

当前 `ExecutionStatus` 包括：

```text
PENDING
RUNNING
PAUSED
WAITING_APPROVAL
WAITING_HUMAN
COMPLETED
FAILED
CANCELLED
```

当前 `StepStatus` 包括：

```text
PENDING
RUNNING
WAITING_APPROVAL
COMPLETED
FAILED_RETRYABLE
FAILED
SKIPPED
```

不存在：

```text
WAITING_DEPENDENCY
WAITING_RETRY
UNKNOWN_RESULT
```

### 5.2 是否需要 `WAITING_DEPENDENCY`

未来**可能需要**。当失败已经分类为暂时依赖不可用，且 Runtime 需要跨 Worker pass、
跨进程或跨服务等待依赖恢复时，单纯 `FAILED` 会把“可恢复等待”表现为终态；
`WAITING_HUMAN` 也不能表达系统依赖等待。

但现在不能直接增加，原因是尚未定义：

- 谁负责健康检查、唤醒和再次领取 execution；
- 等待 deadline、最大等待时长和依赖状态版本；
- 进程重启、租约、并发 worker 和重复唤醒语义；
- API、SSE、前端和持久化层如何呈现该状态；
- 等待期间如何保护未知副作用和已存在的幂等 key。

因此 Phase 10-C 只输出 `WAIT_DEPENDENCY` 决策候选，不修改状态枚举。

### 5.3 是否需要 `WAITING_RETRY`

未来**视 Retry Engine 的调度模型而定**。

- 如果 retry 是同一 Worker pass 内、受预算约束的立即动作，可能不需要额外状态；
- 如果 backoff 会跨进程、跨请求或跨调度周期，必须有一种可恢复、可观察的“已排队
  等待重试”表示，避免 `FAILED_RETRYABLE` 同时表示“可重试”“已请求重试”和
  “等待调度”。

当前已有 `FAILED_RETRYABLE`、`RetryManager` 和显式 `retry_step`，但它们没有
`retry_after`、policy version、依赖等待或稳定的自动调度语义；不能直接把它们当成
完整的 `WAITING_RETRY`。在 Retry Engine 和事件语义确定前，不增加新状态。

### 5.4 是否需要 `UNKNOWN_RESULT`

未知结果首先是**失败事实/副作用风险**，不是现在就应该添加的顶层 Execution 状态：

```text
ExternalAgentFailure.side_effect_state = UNKNOWN
FailureCategory = SIDE_EFFECT_UNKNOWN
RecoveryAction = REQUIRE_RECONCILIATION
```

未来必须有一个持久化、可恢复的对账表示；它可以是专门的 reconciliation work
item，也可以是明确的非终态 step/execution 状态。具体命名（例如
`WAITING_RECONCILIATION` 或 `UNKNOWN_RESULT`）应在对账协议、receipt 查询和 API
投影确定后再决定。

当前直接增加 `UNKNOWN_RESULT` 会造成两个风险：

1. 现有 `is_terminal`、scheduler、`_update_execution_status`、recovery service 和
   前端都不知道如何处理它；
2. “未知写入”可能被错误地当成普通失败并允许重新执行。

所以当前保留 `ExternalAgentFailure` 的 `UNKNOWN` 事实和决策层的
`REQUIRE_RECONCILIATION`，不增加 Execution/Step 枚举。

### 5.5 当前不增加状态的总理由

状态枚举不是分类结果的展示字段。新增任何状态都需要同时定义：

```text
状态进入条件
→ 持久化与版本
→ 事件与 API 投影
→ Worker 唤醒/领取
→ 重启恢复
→ 终态判断
→ 用户/人工控制
```

Phase 10-C 尚未拥有这些实现条件，因此继续使用当前 `FAILED` 行为是有意的限制，
不是将恢复能力偷偷接入。

---

## 6. 副作用安全分析

### 6.1 总规则

自动 retry 必须同时满足：

1. 错误属于暂时性类别，如 `DEPENDENCY_UNAVAILABLE`、`TIMEOUT`、
   `NETWORK_ERROR` 或受控 `RATE_LIMIT`；
2. 请求未越过写入边界，或有权威证据证明 side effect 未开始；
3. ToolContract 声明无副作用，或该操作具备可靠幂等重放能力；
4. 仍有 retry budget、deadline 和租户策略许可；
5. 没有参数、认证、权限、契约或业务拒绝等确定性问题。

任何证据冲突都按保守风险处理。

### 6.2 `request_sent` 三态

| `request_sent` | 只能说明什么 | 安全含义 |
|---|---|---|
| `False` | 当前证据认为请求没有越过该调用边界 | 只有同时有 `NONE/NOT_STARTED` 且无更早子操作副作用时，才是 retry 候选。不能覆盖显式 `POSSIBLE/UNKNOWN`。 |
| `True` | 请求已经越过该调用边界 | 不等于下游已成功，也不等于 side effect 已完成；写操作默认先对账。无副作用的查询可依据 ToolContract 单独评估。 |
| `None` | 无法确认请求是否发出 | 默认按未知处理；写操作禁止盲重试，除非另有权威 ledger/receipt 证明未开始。 |

`None` 不能在 Runtime 适配层被转换成 `False`。当前
`CapabilityExecutor` 和 `InvocationResult` 的布尔字段正是 Phase 10-C 必须解决的
证据传递前提。

### 6.3 `side_effect_state` 组合矩阵

| `side_effect_state` | 典型含义 | 是否允许自动 Retry | 必须动作 |
|---|---|---:|---|
| `NONE` | 明确没有写入或外部副作用 | 仅在暂时性错误、预算、deadline 和 ToolContract 都允许时 | 可生成 `RETRY_IMMEDIATELY` 或 `RETRY_WITH_BACKOFF` 决策。 |
| `NOT_STARTED` | 请求在调用前失败，或有证据证明副作用未开始 | 允许作为候选 | 继续检查 `request_sent`、幂等性、预算和是否存在更早子操作。 |
| `POSSIBLE` | 已越过调用边界，或下游报告可能已执行 | 禁止同一写操作盲重试 | `REQUIRE_RECONCILIATION`；查询 receipt/artifact/幂等状态。 |
| `UNKNOWN` | 无法确认最终结果或写入是否发生 | 禁止 | 先 reconciliation；对账失败时进入人工路径。 |
| `CONFIRMED` | 已有回执/ledger 确认副作用发生 | 禁止同一逻辑操作重放 | 复用已有结果，或执行显式的后续/补偿动作。 |

其中 `CONFIRMED` 虽不是本任务重点输入，但已存在于 Phase 9-B 模型，后续策略不能
丢弃。

### 6.4 四种重点组合

| 请求证据 | 副作用证据 | 结论 |
|---|---|---|
| `False` | `NONE` 或 `NOT_STARTED` | 对暂时性故障可作为安全 retry 候选，但不是自动授权。 |
| `True` | `NONE` 或 `NOT_STARTED` | 如果是无副作用查询，可按 ToolContract 评估；如果是写操作，必须有权威 ledger 证明，否则按证据冲突保守处理。 |
| `True` 或 `None` | `POSSIBLE` 或 `UNKNOWN` | 必须 reconciliation，禁止直接重放同一写操作。 |
| 任意 | `CONFIRMED` | 不重放同一逻辑操作；复用回执/结果或走显式补偿。 |

### 6.5 复合 Tool 的特别边界

`content.create_draft` 不是一个单一外部请求，而是：

```text
Creator submit
→ Creator poll
→ Creator artifact
→ Java POST draft
→ Java GET verify
```

因此某个叶子调用的 `request_sent=False` 只说明该叶子请求的状态，不能自动证明
整个 `content.create_draft` 没有副作用。Java POST 未发出时，Creator task/artifact
可能已经存在；这也是为什么未来 operation-level failure envelope 必须保留：

- 当前失败 phase；
- 已完成的子操作；
- 每个外部系统的 receipt/idempotency key；
- 整体 operation 的 side-effect 汇总状态。

这不是针对 Java 的特殊逻辑，而是所有 composite capability 都必须遵守的通用规则。

### 6.6 ToolContract 元数据的作用

ToolContract 的 `has_side_effect`、`idempotent`、`external_systems` 和
`RetryPolicy` 是 Policy 的约束输入，不是失败事实的替代品：

- `has_side_effect=false` 不能覆盖 `side_effect_state=UNKNOWN`；
- `idempotent=true` 也不能证明上一次写入没有发生；
- `max_attempts` 不能绕过 reconciliation；
- `retryable_error_codes` 不能把所有同码错误都授权为 retry。

---

## 7. `JAVA_BACKEND_UNAVAILABLE` 的通用分析

本错误码只作为下游原始事实和分类输入，不增加专属 Runtime 分支。

通用分类规则是：

```text
JAVA_BACKEND_UNAVAILABLE
    + dependency=java
    + transport/phase/request/side-effect evidence
    → FailureCategory.DEPENDENCY_UNAVAILABLE
       或在结果事实明确未知时归为 SIDE_EFFECT_UNKNOWN
```

### 7.1 可以成为 retry 候选的条件

只有在以下通用条件同时成立时，Policy 才可以输出
`RETRY_IMMEDIATELY` 或 `RETRY_WITH_BACKOFF` 候选：

- `request_sent=False`，且没有被更强的 state/ledger 证据推翻；
- `side_effect_state=NONE` 或 `NOT_STARTED`；
- operation 没有更早已经产生的副作用，或已有可靠的 operation-level 幂等保护；
- ToolContract 和当前租户策略允许；
- attempt budget 和 execution deadline 尚未耗尽。

这只产生 `RecoveryDecision.retry_allowed=true`，当前阶段不执行 retry。

### 7.2 必须 reconciliation 的条件

如果出现以下任一条件，Policy 必须禁止盲重试并要求
`REQUIRE_RECONCILIATION`：

- Java 请求已发送；
- `request_sent=None` 且没有权威的未发送证据；
- `side_effect_state=POSSIBLE/UNKNOWN/CONFIRMED`；
- Java 返回 5xx/连接断开，但无法确认服务是否已处理写请求；
- composite tool 的 Creator 子步骤已成功，而 Java handoff 结果不明；
- ledger、receipt、artifact 或下游状态之间出现冲突。

对账确认“未执行”之后，才可以重新进入一个受预算和幂等键保护的 attempt；确认
“已执行”则应复用已有 draft/receipt，而不是再次创建。

### 7.3 何时 wait 或 manual

- 已确认是暂时依赖不可用、且没有未知副作用时，未来可由 Policy 选择
  `RETRY_WITH_BACKOFF` 或 `WAIT_DEPENDENCY`；
- 连续失败超过预算、依赖长期不健康、deadline 将到期时，未来可选择
  `WAIT_DEPENDENCY` 或 `MANUAL_INTERVENTION`；
- 对账能力缺失、证据冲突无法解决、幂等键丢失时，应选择人工路径，而不是把错误
  降级为普通 dependency wait。

这些动作都由通用类别、证据、ToolContract 和上下文决定；代码中不应出现
`if error_code == "JAVA_BACKEND_UNAVAILABLE"` 的 Worker 特例。

### 7.4 当前阶段的实际结果

即使按上述规则未来可能得到 retry/wait/reconciliation 决策，Phase 10-C 期间当前
运行行为仍保持：

```text
JAVA_BACKEND_UNAVAILABLE
    → 当前 RecoveryPolicy 不识别该 code
    → Execution FAILED
```

本阶段不改变这一点。

---

## 8. 后续实施路线

### Phase 10-C：Failure Decision Integration 设计（本阶段）

目标：确定事实、分类、策略和 Worker 的边界。

产出：

- 当前真实失败链路和证据丢失点；
- Worker-facing decision boundary；
- `FailureClassification`、`FailurePolicyContext`、`RecoveryDecision` 的职责；
- request/side-effect 安全矩阵；
- 不新增状态、不执行恢复的约束。

### Phase 10-D：Worker 消费 `RecoveryDecision`

未来实施重点：

1. 建立无损 failure envelope，从 ToolRuntime/CapabilityExecutor 传递完整失败事实；
2. 在旧 `RecoveryPolicy` 分支前调用 Normalizer、Classifier 和 Policy；
3. Worker 只消费 `RecoveryDecision`，停止按原始 code 自行猜测策略；
4. 统一结构化 failure/decision event payload；
5. 在不增加新状态的前提下，先验证 `FAIL_FAST`、`REQUEST_USER_INPUT` 和安全门行为。

本阶段仍不应对未知写入自动重试。

### Phase 10-E：Retry Engine

未来实施重点：

- bounded attempt、retry budget、backoff 和 deadline；
- ToolContract `RetryPolicy` 与 Runtime Policy 的单一消费关系；
- 稳定 logical operation id 和幂等 key；
- retry requested/started/completed/exhausted 事件；
- `NONE/NOT_STARTED` 的安全重试和证据冲突 fail-closed；
- `POSSIBLE/UNKNOWN` 写操作明确禁止盲重放。

### Phase 10-F：Dependency Waiting

未来实施重点：

- 依赖健康状态、等待窗口、唤醒和 deadline；
- `WAITING_DEPENDENCY` 是否作为 Execution/Step 状态或独立调度记录；
- 进程重启、租约、并发领取和重复唤醒；
- API/SSE/前端投影和用户可见文案；
- 等待与未知副作用/对账任务的边界。

### Phase 10-G：Reconciliation

未来实施重点：

- 持久化 operation-level side-effect ledger；
- receipt、artifact、幂等状态和下游查询协议；
- Creator/Java/MCP 多阶段操作的部分成功收敛；
- `POSSIBLE`、`UNKNOWN`、`CONFIRMED` 的最终处理；
- 对账确认未执行后的安全重试、已执行后的结果复用，以及无法确认时的人工接管；
- 不允许用“重试成功”覆盖先前未知副作用。

---

## 9. 本阶段完成判定与限制

- 仅新增本设计文档；未修改业务代码。
- 未接入 `FailureNormalizer`、`FailureClassifier`、`FailurePolicy` 或
  `RecoveryDecision` 到 Runtime。
- 未实现 retry、dependency wait、reconciliation 或自动恢复。
- 未新增 `WAITING_DEPENDENCY`、`WAITING_RETRY`、`UNKNOWN_RESULT` 状态。
- 未修改 Worker、RuntimeAgentService、Planner、ToolRuntime、ExecutionStateManager、
  ToolContract、Creator Agent 或 Execution 状态模型。
- 未针对 `JAVA_BACKEND_UNAVAILABLE` 编写特殊逻辑；该错误只作为通用
  `DEPENDENCY_UNAVAILABLE`/未知副作用规则的示例。

后续实现的首要门禁不是“先把所有失败都变成可重试”，而是先保证 failure fact
无损、策略 fail-closed，并明确 retry、wait 和 reconciliation 的互斥安全边界。
