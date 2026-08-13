# Phase 10-A：Failure Classification 设计方案

## 0. 范围与结论

本阶段是只读架构设计，不修改 Runtime 业务代码，不启动服务，也不改变当前
`Execution`/`StepExecution` 状态流转。

当前已经有一层把 `ToolResult` 规范化为 `ExternalAgentFailure` 的 contracts
基础设施，但它还没有接入 `RuntimeAgentService`、`ExecutionWorker` 或
`ExecutionEventStore`。因此本阶段的结论是：

> `JAVA_BACKEND_UNAVAILABLE` 现在只能被保存为原始失败并导致当前的
> `FAILED`；它还没有统一的分类、风险标记和恢复决策消费点。

Phase 10-A 只定义分类事实和边界。`retry`、依赖等待、状态迁移、补偿和
reconciliation 留给后续阶段。

---

## 1. 当前 `ExternalAgentFailure` 模型与生命周期

### 1.1 代码位置

| 组件 | 路径 | 当前职责 |
|---|---|---|
| `ExternalAgentFailure`、`SideEffectState`、`RecoveryAction` | `packages/contracts/greenbook_contracts/external_agent_failure.py` | 以不可变 Pydantic 模型承载下游失败事实和规范化恢复提示 |
| `normalize_external_failure` / `FailureNormalizer` | 同上 | 将失败的 `ToolResult` 转换成上述模型的纯函数/静态门面 |
| `ToolResult` | `packages/contracts/greenbook_contracts/tool_result.py` | Tool/MCP/Creator/Java 层之间的原始结果契约 |
| 单元测试 | `tests/unit/test_external_agent_failure.py` | 验证错误码保留、`request_sent` 三态、side-effect 推导和认证安全默认值 |

### 1.2 字段

当前字段如下。除 `dependency`、`error_code` 等核心字段外，后续分类不能丢失
请求生命周期证据。

| 字段 | 来源/含义 | 分类用途 |
|---|---|---|
| `dependency` | 显式传入、错误码映射或 `unknown` | 判断失败发生在哪个外部边界 |
| `error_code` | 原始 `ToolResult.code`，大小写和值保持不变 | 最可靠的可审计事实，不能被类别覆盖 |
| `retryable` | `ToolResult` 的提示，认证类由 normalizer 强制为 `false` | 仅作为分类输入提示，不是执行授权 |
| `user_visible_message` | `ToolResult.user_message` 优先，否则使用内部消息 | 未来 Presenter 的用户提示 |
| `recovery_action` | normalizer 当前给出的 `RETRY`/`WAIT_DEPENDENCY`/`RECONCILE`/`REAUTH`/`FAIL` | 现阶段是建议，不执行动作 |
| `request_sent` | `False`、`True` 或 `None` | 判断请求是否越过外部边界；`None` 表示无法确认 |
| `side_effect_state` | `NONE`、`NOT_STARTED`、`POSSIBLE`、`UNKNOWN`、`CONFIRMED` | 判断是否可以安全地重放写操作 |
| `message`、`phase` | 原始消息及 PRE/POST/下游阶段 | 区分参数校验、调用中断和结果校验 |
| `trace_id`、`receipt_id` | 追踪标识或下游回执 | 未来诊断/对账关联 |
| `idempotency_key` | 外部写操作的幂等键（若已有） | future retry/reconciliation 的安全前提 |
| `metadata` | 原始 state 和 transport 证据 | 保留状态码、异常类型、attempt 等扩展信息 |

`SideEffectState.CONFIRMED` 已存在于模型，即使早期测试主要覆盖其余四种
状态，分类器必须保留这一状态。

### 1.3 当前生命周期

当前真实链路是：

```text
Creator/Java/MCP handler
    -> ToolResult(ok=False, code, retryable, request_sent, state)
    -> normalize_external_failure(...)
    -> ExternalAgentFailure (纯内存对象)
```

目前 `FailureNormalizer` 只在 contracts 测试和设计层使用，代码扫描没有发现
它已经被 Runtime 执行链路消费。真实 Runtime 链路仍然是：

```text
CapabilityExecutor -> ExecutionResult(error_code, retryable, request_sent)
                   -> ExecutionWorker/RuntimeAgentService
                   -> StepExecution FAILED / Execution FAILED
```

也就是说：

- `ExecutionRepository` 不保存 `ExternalAgentFailure`；
- `ExecutionEventStore` 不产生 `FailureClassification` 事件；
- `ToolExecutionLedger` 目前是 `RuntimeAgentService._execute_single` 内的局部
  对象，不是持久化账本；
- 当前 Worker 依据原始错误码调用
  `packages/assistant_core/greenbook_assistant_core/execution/recovery.py` 中的
  `RecoveryPolicy`，尚未依据 `ExternalAgentFailure` 分类。

因此本阶段不会宣称分类能力已经生效，只提供下一阶段可以接入的稳定语义。

---

## 2. Runtime 失败来源审计

下表列出当前代码中已经存在或可观测的失败边界。这里的“建议分类”是未来
`FailureClassifier` 的输出，不改变当前错误码和当前状态。

| 层 | 主要代码位置 | 代表性错误 | 建议分类 | 副作用证据 |
|---|---|---|---|---|
| IntentSpecProvider | `packages/assistant_core/greenbook_assistant_core/task/intent_spec_provider.py` | `INTENT_UNSUPPORTED`、`INTENT_SPEC_INVALID`、`INTENT_VALIDATION_FAILED` | `INVALID_ARGUMENT` | 尚未进入 Tool，`NONE/NOT_STARTED` |
| Intent L2/模型调用 | 同上及 `TaskUnderstanding` | 模型超时、网络异常、解析失败 | `TIMEOUT`、`NETWORK_ERROR` 或 `INVALID_ARGUMENT` | 语义理解阶段无业务写副作用 |
| TaskProvider | `apps/assistant_api/greenbook_assistant_api/services/task_provider.py` | `TASK_SCOPE_INVALID`、`TASK_SCOPE_MISMATCH`、`TASK_TARGET_REQUIRED`、`TASK_NOT_FOUND`、`TASK_TARGET_AMBIGUOUS`、`TASK_STATE_CONFLICT`、`TASK_PROVIDER_UNAVAILABLE` | 权限/输入错误、`NOT_FOUND`、`STATE_CONFLICT` 或 `DEPENDENCY_UNAVAILABLE` | 查询为 `NONE`；创建事务若提交结果不明需标记 `UNKNOWN` |
| IntentCompiler | `apps/assistant_api/greenbook_assistant_api/services/intent_compiler.py` | `INTENT_REQUIRED`、`GOAL_REQUIRED`、`TASK_CONTEXT_REQUIRED`、`TASK_CONTEXT_MISMATCH`、`ARTIFACT_NOT_FOUND`、`AMBIGUOUS_TARGET` | `INVALID_ARGUMENT`、`NOT_FOUND` 或 `STATE_CONFLICT` | 编译纯函数，无 Tool 副作用 |
| ArgumentBinder | `packages/assistant_core/greenbook_assistant_core/execution/argument_binder.py` | 缺少必填字段、时间/类型解析异常、未知 capability/schema | `INVALID_ARGUMENT`；schema 漂移为 `CONTRACT_MISMATCH` | 绑定发生在 Tool 调用前，`NOT_STARTED` |
| ToolContract 输入校验 | `services/greenbook_mcp/greenbook_mcp_server/server.py` | `VALIDATION_ERROR`、`INVALID_TOOL_ARGUMENT`、`TOOL_ARGUMENT_VALIDATION_FAILED`、`PRE_EXECUTION_VALIDATION_FAILED` | `INVALID_ARGUMENT`；注册 schema/handler 不一致为 `CONTRACT_MISMATCH` | `request_sent=False`、`side_effect_started=False` |
| MCP 路由/执行 | 同上及 `services/greenbook_mcp/.../tool_registry.py` | `MCP_TIMEOUT`、MCP 连接失败、`UNKNOWN_TOOL`、`MISSING_TOOL`、`NO_HANDLER`、`TOOL_EXECUTION_FAILED` | `TIMEOUT`、`NETWORK_ERROR`、`DEPENDENCY_UNAVAILABLE` 或 `UNKNOWN` | handler 是否执行由 phase/downstream_called 决定 |
| ToolContract 输出校验 | `server.py` POST_EXECUTION 分支 | `TOOL_OUTPUT_VALIDATION_FAILED` | `CONTRACT_MISMATCH` | 下游已调用；至少 `POSSIBLE`，不能按普通参数错误重试 |
| CreatorClient | `services/greenbook_mcp/greenbook_mcp_server/clients/creator_client.py` | `CREATOR_UNAVAILABLE`、`CREATOR_TIMEOUT`/`TIMEOUT`、任务失败、`NOT_FOUND`、认证或限流错误 | `DEPENDENCY_UNAVAILABLE`、`TIMEOUT`、`BUSINESS_REJECTED`、`NOT_FOUND`、`AUTH_FAILURE` 或 `RATE_LIMIT` | 提交前 `NOT_STARTED`；已创建任务但轮询超时为 `POSSIBLE/UNKNOWN` |
| `content.create_draft` | `services/greenbook_mcp/greenbook_mcp_server/tools/content.py` | Creator submit/poll、artifact 缺失、Java 写入/校验失败 | 继承下游分类 | 可能已经创建 Creator artifact 或 Java draft |
| Java Agent Facade 客户端 | `services/greenbook_mcp/greenbook_mcp_server/clients/java_client.py` | `JAVA_BACKEND_UNAVAILABLE`、`RESULT_UNKNOWN`、`TIMEOUT`、`REQUEST_NOT_SENT`、`AUTHENTICATION_FAILED`、`AUTHORIZATION_DENIED`、`DOWNSTREAM_VALIDATION_FAILED`、冲突码 | 依赖不可用、`SIDE_EFFECT_UNKNOWN`、超时、认证、权限、`INVALID_ARGUMENT`/`STATE_CONFLICT` | 连接失败通常未发出；写入读超时/5xx 的请求结果可能不明 |
| HTTP transport | Creator/Java client `_request` | Connect/Pool/RemoteProtocol/Network、Read/Write timeout、非 2xx | `NETWORK_ERROR`、`TIMEOUT`、依据 HTTP 状态进一步分类 | write timeout/读响应超时不能假设未写入 |
| Assistant API JWT | `apps/assistant_api/greenbook_assistant_api/main.py`、`routes.py` | 缺 token、格式错误、`invalid_audience`、过期、签名无效 | `AUTH_FAILURE` | 请求通常在业务 Tool 前被拦截，`NONE` |
| Execution/Task ownership | `runtime_routes.py` 及 TaskProvider scope 校验 | `401`、`403`、`TASK_SCOPE_MISMATCH`、`AUTHORIZATION_DENIED` | `AUTH_FAILURE` 或 `PERMISSION_DENIED` | 没有业务 Tool 副作用 |
| Runtime 编排 | `runtime_agent_service.py`、`conversation_runtime_adapter.py` | `TASK_CONTEXT_REQUIRED`、`NO_CAPABILITY`、`PLAN_INVALID`、`RUNTIME_ADAPTER_FAILED`、`RUNTIME_ERROR` | `INVALID_ARGUMENT`、`CONTRACT_MISMATCH` 或 `UNKNOWN` | 通常发生在 Tool 前；若 detached execution 已启动需看 ledger |
| Worker/RecoveryPolicy | `packages/assistant_core/greenbook_assistant_core/execution/worker.py`、`recovery.py` | 当前按 `TIMEOUT`、`NETWORK_ERROR`、`RATE_LIMIT`、`TEMPORARY_UNAVAILABLE` 判断 | 未来消费分类，不应丢失原始码 | 重试前必须结合 `side_effect_state` |
| Persistence/EventStore | Execution repository、`ExecutionEventStore` | 数据库连接/事务失败、事件写入失败、并发冲突 | `DEPENDENCY_UNAVAILABLE`、`NETWORK_ERROR`、`STATE_CONFLICT` 或 `UNKNOWN` | 读失败通常无外部副作用；写事务结果不明需对账 |

### 2.1 输入错误与系统故障必须分开

同一个 HTTP 500 不能自动等同于可重试。分类必须同时读取：

1. 原始 `error_code`；
2. `phase`（调用前、调用中、调用后）；
3. `request_sent` 三态；
4. `side_effect_state`；
5. `dependency` 和 transport metadata；
6. ToolContract 的 side-effect 元数据。

例如，MCP 在调用前发现缺字段和 Java 在写入后读超时都可能表现为“工具失败”，
但前者是 `INVALID_ARGUMENT` 且可安全修正，后者必须优先保护未知副作用。

---

## 3. 统一 `FailureCategory`

### 3.1 建议类别

`FailureCategory` 是对原始错误的稳定分类，不替换 `error_code`。建议的最小集合
如下（括号内是默认恢复提示，而不是立即执行的动作）：

| 类别 | 判定依据 | 默认 `retryable` 建议 | 副作用风险 | 人工介入 |
|---|---|---:|---|---:|
| `INVALID_ARGUMENT` | 输入缺失、类型/语义不合法、用户目标不可解析，且调用尚未越过 Tool 边界 | 否 | `NONE`/`NOT_STARTED` | 否；需用户修正 |
| `AUTH_FAILURE` | JWT/下游凭证缺失、过期、签名/audience 无效、401 | 否，先 `REAUTH` | 通常 `NONE` | 配置/凭证持续错误时是 |
| `PERMISSION_DENIED` | scope、owner、角色、审批权限不满足，403 | 否 | `NONE` | 需要用户授权或管理员 |
| `DEPENDENCY_UNAVAILABLE` | 外部 Agent、MCP、数据库不可达、连接失败、暂时 5xx | 仅在未发送且无副作用证据时可作为候选 | `NONE` 至 `POSSIBLE` | 依赖长期不可用时是 |
| `TIMEOUT` | connect/read/write/poll deadline 超时，且不是凭证或限流问题 | 未发送时可候选；已发送时先对账 | 可能 `POSSIBLE/UNKNOWN` | 超过策略上限时是 |
| `RATE_LIMIT` | 429、`RATE_LIMIT`、`TOO_MANY_REQUESTS` 及明确限流头 | 是延迟候选，不是立即重试 | 通常 `NONE`；写请求也需看证据 | 否，策略耗尽时是 |
| `NETWORK_ERROR` | DNS、连接、协议、传输断开等非业务错误 | 未发送时可候选 | 已发送时 `UNKNOWN` 可能 | 否；反复发生时是 |
| `CONTRACT_MISMATCH` | Tool 输入/输出 schema、handler 签名、版本契约不一致 | 否 | 前置校验为 `NONE`；后置校验为 `POSSIBLE` | 是，需开发/运维修复 |
| `SIDE_EFFECT_UNKNOWN` | `RESULT_UNKNOWN`、请求已发但无确认、账本与回执不一致 | 否，禁止盲目重放 | `UNKNOWN` | 是，需要 reconciliation |
| `NOT_FOUND` | Task、artifact、下游资源不存在 | 否 | 通常 `NONE` | 用户目标错误时需用户修正 |
| `STATE_CONFLICT` | 版本冲突、任务状态不允许、幂等键冲突 | 否；可提供显式修改动作 | 可能已存在已有写入 | 用户或人工选择下一版本 |
| `BUSINESS_REJECTED` | Creator/Java 已处理但业务规则拒绝 | 否 | 结果通常已明确 | 需要用户改目标或审批 |
| `UNKNOWN` | 无法从已有证据确定根因 | 否 | `UNKNOWN`，除非明确无请求 | 是，至少记录 trace 并人工诊断 |

### 3.2 分类优先级

为避免“通用网络错误”覆盖安全或副作用事实，分类器按以下顺序决策：

1. **权威安全结果**：401/凭证错误 → `AUTH_FAILURE`；403/scope 错误 →
   `PERMISSION_DENIED`。
2. **前置输入/契约结果**：Tool 尚未执行时的参数校验 → `INVALID_ARGUMENT`；
   schema/handler/输出版本不一致 → `CONTRACT_MISMATCH`。
3. **未知写入结果**：原始错误表示结果未知，或请求已发且 side effect 为
   `POSSIBLE/UNKNOWN` 而没有回执 → `SIDE_EFFECT_UNKNOWN`（也保留根因于
   `raw_error_code`/metadata）。
4. **可识别的传输/依赖错误**：超时 → `TIMEOUT`；429 → `RATE_LIMIT`；连接/协议
   错误 → `NETWORK_ERROR`；外部服务不可用/5xx → `DEPENDENCY_UNAVAILABLE`。
5. **资源/业务/状态错误**：分别为 `NOT_FOUND`、`BUSINESS_REJECTED`、
   `STATE_CONFLICT`。
6. **其余情况** → `UNKNOWN`。

`SIDE_EFFECT_UNKNOWN` 既可以作为主类别（如 `RESULT_UNKNOWN`），也可以作为
`side_effect_risk` 标记附着在 `DEPENDENCY_UNAVAILABLE`/`TIMEOUT` 上。实现时不应
为了显示一个类别而丢失 `JAVA_BACKEND_UNAVAILABLE` 等原始码。

### 3.3 副作用风险值

建议 `FailureClassification.side_effect_risk` 复用 `SideEffectState` 的语义：

- `NONE`：确定没有执行写入；
- `NOT_STARTED`：请求在调用前失败；
- `POSSIBLE`：已越过调用边界或下游报告可能已执行；
- `UNKNOWN`：无法确认结果；
- `CONFIRMED`：已有回执/账本确认写入。

这不是 Execution 状态，也不是重试许可。它只回答“再次调用是否有重复副作用
风险”。

---

## 4. `FailureClassifier` 设计

### 4.1 输入与输出

建议的纯函数接口：

```text
FailureClassifier.classify(
    failure: ExternalAgentFailure,
) -> FailureClassification
```

输出模型建议如下：

```text
FailureClassification {
    category: FailureCategory
    raw_error_code: str
    dependency: str
    retryable: bool
    recovery_action: RecoveryAction
    side_effect_risk: SideEffectState
    requires_human: bool
    rationale: str
}
```

`raw_error_code` 必须等于输入的 `ExternalAgentFailure.error_code`。若需要别名
匹配，只在内部使用大写/别名，不修改原始值。`recovery_action` 复用现有
`RecoveryAction`，但在 Phase 10-A 仍然只是分类建议。

### 4.2 分类器职责

分类器只做以下事情：

1. 根据 error code、dependency、phase、transport metadata 识别根因类别；
2. 将 `request_sent` 和 `side_effect_state` 映射为 side-effect 风险；
3. 计算“在满足安全前提时是否具备重试候选资格”；
4. 给出 `REAUTH`、`RECONCILE`、`WAIT_DEPENDENCY`、`RETRY` 或 `FAIL` 的建议；
5. 保留原始错误、回执、trace 和幂等键，生成可审计的 rationale。

分类器明确**不**做：

- 发起 retry、sleep、backoff 或调用任何服务；
- 写 `ExecutionRepository`、`ExecutionEventStore` 或 Side Effect Ledger；
- 修改 `Execution`/`StepExecution` 状态；
- 绕过 ToolContract、权限或审批；
- 生成新的用户成功文案。

### 4.3 决策伪代码

```text
classify(failure):
    risk = derive_side_effect_risk(failure.request_sent,
                                   failure.side_effect_state)

    if is_authentication(failure):
        return AUTH_FAILURE, retryable=False, REAUTH, risk
    if is_permission(failure):
        return PERMISSION_DENIED, retryable=False, FAIL, risk
    if is_pre_execution_argument(failure):
        return INVALID_ARGUMENT, retryable=False, FAIL, risk
    if is_contract_mismatch(failure):
        return CONTRACT_MISMATCH, retryable=False, FAIL, risk
    if result_is_unknown(failure) or risk in {POSSIBLE, UNKNOWN} \
       and no_receipt(failure):
        return SIDE_EFFECT_UNKNOWN, retryable=False, RECONCILE, risk
    if is_rate_limit(failure):
        return RATE_LIMIT, retryable=True, WAIT_DEPENDENCY, risk
    if is_timeout(failure):
        return TIMEOUT, retryable=(risk in {NONE, NOT_STARTED}), \
               (RETRY if safe else RECONCILE), risk
    if is_network(failure):
        return NETWORK_ERROR, retryable=(risk in {NONE, NOT_STARTED}), \
               (RETRY if safe else RECONCILE), risk
    if is_dependency_unavailable(failure):
        return DEPENDENCY_UNAVAILABLE, retryable=(risk in {NONE, NOT_STARTED}), \
               (RETRY if safe else RECONCILE), risk
    ...
    return UNKNOWN, retryable=False, FAIL, risk
```

上述 `retryable` 是“分类结果中的安全候选标记”，不是对 Worker 的执行指令。
后续 `FailurePolicy` 仍需结合 attempt、预算、ToolContract 和运行环境重新决定。

---

## 5. 关键场景分类矩阵

| 场景 | 原始证据 | `category` | `retryable` 建议 | `recovery_action` | `side_effect_risk` |
|---|---|---|---:|---|---|
| Java 连接失败/5xx：`JAVA_BACKEND_UNAVAILABLE`，确认未发出 | `request_sent=False`、无写入回执 | `DEPENDENCY_UNAVAILABLE` | 是（候选） | `RETRY` | `NOT_STARTED` |
| Java 同码但请求已发或状态未知 | `request_sent=True/None`，无 receipt | `DEPENDENCY_UNAVAILABLE`（风险标记为未知） | 否，禁止盲重放 | `RECONCILE` | `POSSIBLE/UNKNOWN` |
| Creator poll/HTTP 超时：`CREATOR_TIMEOUT` | 已提交任务后轮询超时 | `TIMEOUT` | 否，先查回执 | `RECONCILE` | `POSSIBLE/UNKNOWN` |
| MCP 参数错误：`TOOL_ARGUMENT_VALIDATION_FAILED` | PRE_EXECUTION、未调用 handler | `INVALID_ARGUMENT` | 否 | `FAIL` | `NOT_STARTED` |
| MCP schema/handler 版本漂移 | 输入/输出或签名不一致 | `CONTRACT_MISMATCH` | 否 | `FAIL` | 前置为 `NONE`，后置为 `POSSIBLE` |
| JWT 缺失/`invalid_audience`/401 | API 或下游凭证拒绝 | `AUTH_FAILURE` | 否，刷新凭证后再决定 | `REAUTH` | `NONE` |
| execution/task scope 不匹配/403 | owner、tenant 或角色校验失败 | `PERMISSION_DENIED` | 否 | `FAIL` | `NONE` |
| 数据库连接失败（读取/写入前） | connection/network error，未提交 | `DEPENDENCY_UNAVAILABLE` 或 `NETWORK_ERROR` | 是（候选） | `RETRY` | `NOT_STARTED` |
| 数据库事务提交结果不明 | commit 后断连，无确认 | `SIDE_EFFECT_UNKNOWN` | 否，先对账 | `RECONCILE` | `UNKNOWN` |
| 数据库约束/版本冲突 | 明确的 constraint/version 错误 | `STATE_CONFLICT` | 否 | `FAIL` | 可能存在既有写入 |

### 5.1 `JAVA_BACKEND_UNAVAILABLE` 的通用结论

该错误码由 Java client 的连接失败或下游 5xx 映射而来，根因类别是
`DEPENDENCY_UNAVAILABLE`，而不是专为某个帖子请求增加的特殊分支。

分类结果必须同时携带 side-effect 风险：

- 明确 `request_sent=False`、`side_effect_state=NOT_STARTED/NONE`：可以标记为
  `retryable=True` 的重试候选；
- `request_sent=True`，或 Java 返回 5xx 但没有可靠的请求处理证据：保留根因
  `DEPENDENCY_UNAVAILABLE`，将风险设为 `POSSIBLE/UNKNOWN`，动作建议为
  `RECONCILE`，不能直接重放；
- 若原始码是 `RESULT_UNKNOWN`，主类别提升为 `SIDE_EFFECT_UNKNOWN`，并继续保留
  `JAVA_BACKEND_UNAVAILABLE`（如果它存在于 metadata）作为原始下游事实。

因此，分类器不会把所有 Java 错误都标成“可自动重试”，也不会把所有错误都改成
`FAILED` 以外的状态。当前状态保持不变。

---

## 6. `FailureClassifier` 与 `FailurePolicy` 的边界

### FailureClassifier（Phase 10-A）

- 输入：一个已经规范化的 `ExternalAgentFailure`；
- 输出：稳定、可审计的类别、风险、建议动作和安全候选标记；
- 性质：纯函数、确定性、无 I/O、无计时器、无状态写入；
- 关注：**发生了什么**以及**当前证据允许怎样理解它**。

### FailurePolicy（后续 Phase 10-B/10-D）

- 输入：`FailureClassification` 加上 step/tool contract、attempt 次数、重试预算、
  用户/租户策略、当前 execution 状态和 ledger/reconciliation 结果；
- 输出：是否排队重试、等待依赖、暂停审批、人工接管、对账或终止；
- 负责：backoff、最大次数、幂等键要求、限流窗口和策略审计；
- 关注：**现在应该采取什么动作**。

`ExternalAgentFailure.recovery_action` 是 Phase 9-B normalizer 的保守提示，不能
被当作 `FailurePolicy` 的最终授权。特别是 `RETRY` 不能绕过副作用风险检查，
`RECONCILE` 不能在当前阶段自行调用下游。

---

## 7. 未来 Worker 接入点（仅设计）

当前 `ExecutionWorker._execute_one_step` 仍按原始 `error_code` 调用
`RecoveryPolicy.can_retry_failure`。未来最小接入点应位于：

```text
CapabilityExecutor.execute(step)
    -> ExecutionResult(ok=False, error_code, request_sent, state)
    -> normalize_external_failure(ToolResult)
    -> FailureClassifier.classify(ExternalAgentFailure)
    -> FailurePolicy.decide(classification, step/tool/attempt/ledger)
    -> Worker 执行“重试/等待/对账/失败”之一
```

此处的顺序很重要：分类必须发生在 Worker 依据错误码作恢复判断之前；但在
Phase 10-A 不接入上述调用，也不改变 Worker 的当前失败路径。未来还应把分类结果
作为 step event payload 的结构化字段，而不是仅拼接到用户文案中。

---

## 8. 最小演进路线

| 阶段 | 目标 | 允许的变化 | 明确不做 |
|---|---|---|---|
| Phase 10-A | `ExternalAgentFailure` → `FailureClassification` 统一分类 | 新增 contracts 模型、别名表、纯函数测试和本设计文档 | 不接 Worker、retry、等待状态或事件写入 |
| Phase 10-B | 恢复策略设计/实现 | 引入 `FailurePolicy`，定义预算、backoff、对账前置条件 | 不直接放宽所有失败的 retry |
| Phase 10-C | Worker 消费分类 | 在 CapabilityExecutor 与现有 RecoveryPolicy 之间接入 classifier/policy，保留原始码 | 不修改 Planner/ToolContract 核心语义 |
| Phase 10-D | 有界 Retry | 仅对安全类别和幂等操作执行重试，产生 retry 事件 | 不对 `POSSIBLE/UNKNOWN` 写操作盲重放 |
| Phase 10-E | Reconciliation | 持久化 side-effect ledger、receipt 查询、幂等键和未知结果恢复 | 不用“重试成功”掩盖未知副作用 |

---

## 9. 本阶段验收与限制

- 本阶段只新增本设计文档；没有修改 Runtime、MCP、Creator、数据库或状态模型。
- 没有接入 `Retry`、`WAITING_DEPENDENCY` 或新的 Execution 状态。
- 没有针对“Java 发布帖子”增加特例；该案例仅用于说明通用分类规则。
- 后续实现必须保留原始 `error_code`、`request_sent`、`side_effect_state`、
  `trace_id` 和 `idempotency_key`，否则分类无法支持安全恢复。

