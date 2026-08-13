# Phase 10-B：Failure Policy 恢复策略设计方案

## 0. 范围与结论

本阶段只做恢复策略的只读设计，不修改 Runtime 业务代码，不启动服务，不运行
完整测试，也不改变现有 `Execution`/`StepExecution` 状态模型。

Phase 10-A 解决的是“失败是什么”（`FailureClassification`）。Phase 10-B 解决
的是“在当前证据和策略约束下，未来允许采取什么动作”（`FailurePolicy`）。本阶段
不执行这些动作：不重试、不进入 `WAITING_DEPENDENCY`、不对账、不自动恢复。

核心结论：

> 不能把 `FailureClassification.retryable` 或 normalizer 的
> `recovery_action` 直接当成 Worker 的执行指令。恢复决策还必须结合请求是否
> 发出、side effect 状态、工具副作用元数据、幂等能力、attempt 预算、租户策略
> 和全局 deadline。

---

## 1. 当前分类输出审计

### 1.1 已有事实模型

`ExternalAgentFailure` 位于
`packages/contracts/greenbook_contracts/external_agent_failure.py`，由
`normalize_external_failure(ToolResult)` 纯函数生成。当前字段为：

- `dependency`
- 原始 `error_code`
- `retryable`
- `user_visible_message`
- `recovery_action`
- `request_sent: bool | None`
- `side_effect_state: NONE | NOT_STARTED | POSSIBLE | UNKNOWN | CONFIRMED`
- `message`、`phase`
- `trace_id`、`receipt_id`、`idempotency_key`
- `metadata`

normalizer 会保留原始错误码，依据 state 和 `request_sent` 推导副作用状态，给出
一个保守的恢复提示；它不写 Execution、不调用下游、不发起 retry。

### 1.2 Phase 10-A 的 `FailureClassification`

Phase 10-A 设计的输出形状为：

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

它已经足以完成统一分类和基本安全提示，但不足以独立支撑最终恢复决策：

1. `retryable` 只表达错误本身的候选属性，没有 attempt、预算和 deadline；
2. `side_effect_risk` 是风险摘要，若不同时保留原始
   `request_sent`/`side_effect_state`，会丢失关键证据；
3. 没有明确指出错误来自哪一个执行阶段和哪一个 Tool/Capability；
4. 没有携带 ToolContract 的 `has_side_effect`、幂等支持、允许的最大尝试次数
   等操作约束；
5. 没有 `retry_after`、backoff 上限、策略版本和决策原因等策略输出；
6. 没有将“需要对账”与“可以重试”建模为互斥的安全门；
7. 没有区分用户可修正、系统配置错误和需要人工调查的情况。

### 1.3 最小补充设计

不建议把所有策略上下文塞进 `FailureClassification`。更稳定的边界是：

```text
FailureClassification（事实分类，尽量稳定）
    + FailurePolicyContext（执行时上下文）
    -> RecoveryDecision（本次策略决定）
```

建议 `FailureClassification` 至少保留以下证据字段（可以通过引用或只读投影
暴露，不要求本阶段改模型）：

| 字段 | 用途 |
|---|---|
| `source` / `phase` | 区分 Intent、MCP PRE_EXECUTION、下游调用、POST_EXECUTION |
| `capability` / `tool_name` | 读取 ToolContract 副作用和幂等元数据 |
| `request_sent` | 保留三态，不能压缩为布尔值 |
| `side_effect_state` | 保留实际证据，不只保留 risk 摘要 |
| `idempotency_key`、`receipt_id`、`trace_id` | 支持后续对账和安全重试 |
| `evidence`/`rationale` | 记录分类所依据的状态码、异常类型和 state |

建议另设不可变的 `FailurePolicyContext`，而不是让分类器读取运行时全局状态：

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
    source: str
    user_requested_retry: bool
    policy_version: str
}
```

`attempt`、预算和 deadline 属于策略输入，不属于“错误是什么”。这样可以保证
同一错误在不同工具、不同租户策略下得出不同动作，而不改变错误分类本身。

---

## 2. FailureClassifier 与 FailurePolicy 的边界

### 2.1 FailureClassifier：回答“这个错误是什么”

职责：

- 根据原始错误码、dependency、phase、HTTP/transport metadata 和
  `request_sent`/side-effect 证据，确定 `FailureCategory`；
- 输出副作用风险和保守的候选动作；
- 保留原始错误码、回执和 trace；
- 保持确定性、无 I/O、无执行副作用。

它不应该知道本次 execution 已重试几次，也不应该决定是否真的向 Java、Creator
或 MCP 发起下一次请求。

### 2.2 FailurePolicy：回答“遇到这个错误应该采取什么策略”

职责：

- 读取 `FailureClassification` 与 `FailurePolicyContext`；
- 应用 ToolContract 的副作用/幂等声明、attempt 预算、backoff、全局 deadline、
  用户/租户限制和人工接管规则；
- 输出一次不可执行的 `RecoveryDecision`，由未来 Worker 决定是否消费；
- 明确重试、等待、对账、请求用户输入、人工介入或快速失败的边界。

### 2.3 不能合并的原因

1. **事实和策略变化速度不同**：错误分类应稳定，重试预算和限流策略会随部署
   或租户改变。
2. **安全隔离**：分类器给出 `retryable=True` 不能越过副作用风险和幂等检查；
   独立 Policy 可强制 fail-closed。
3. **可测试性**：分类器可以纯函数穷举测试，Policy 可独立测试预算/backoff/期限。
4. **跨依赖复用**：`DEPENDENCY_UNAVAILABLE` 适用于 Java、Creator、MCP 和数据库，
   不应在分类器中写每个服务的重试分支。
5. **审计与回放**：同一分类事实可以在策略版本变化后重新评估，不需要伪造原始
   错误码。

---

## 3. `RecoveryAction` 设计

以下枚举是策略输出，不代表 Phase 10-B 立即改变 Execution 状态。动作本身应为
纯数据，实际状态迁移由未来 Worker/Runtime 生命周期层完成。

| Action | 适用错误类型 | 未来状态影响 | 副作用要求 | 人工参与 |
|---|---|---|---|---:|
| `NO_ACTION` | 已有最终结果、重复事件、策略不需要动作 | 保持当前状态 | 不产生新调用 | 否 |
| `RETRY_IMMEDIATELY` | 明确未发出、无副作用的瞬时故障 | 未来可重新排队 Step | 必须 `request_sent=False` 且 `NONE/NOT_STARTED`；最好是幂等工具 | 否 |
| `RETRY_WITH_BACKOFF` | 依赖暂时不可用、网络错误、可等待的超时、限流 | 未来可进入重试调度 | 仍需安全的 side-effect 证据和剩余预算 | 否 |
| `WAIT_DEPENDENCY` | 依赖健康检查失败、限流窗口未到、维护窗口 | 未来可能映射为依赖等待状态；本阶段不执行 | 已有调用结果未知时不能代替 reconciliation | 否 |
| `REQUEST_USER_INPUT` | 参数错误、目标歧义、缺失审批/业务输入 | 未来暂停等待用户补充 | 不应再次自动调用有副作用 Tool | 需要用户 |
| `REQUIRE_RECONCILIATION` | `RESULT_UNKNOWN`、请求已发但没有回执、账本不一致 | 未来暂停写操作，先查 receipt/ledger | 禁止盲目重放 | 通常需要系统；失败时人工 |
| `MANUAL_INTERVENTION` | 契约漂移、持续认证配置错误、对账无法确定 | 未来转人工队列或终止 | 由人工确认后才可继续 | 是 |
| `FAIL_FAST` | 权限拒绝、确定性非法参数、业务拒绝、不可修复冲突 | 未来标记 Step/Execution 失败 | 不发起调用 | 通常不需要；配置类需人工 |

同一类别可能产生不同动作。例如 `TIMEOUT` 在请求尚未发出时可以
`RETRY_WITH_BACKOFF`，在写请求已经发出时必须 `REQUIRE_RECONCILIATION`。

### 3.1 Action 是否修改 Execution 状态

Phase 10-B 的 `RecoveryDecision` 不修改任何状态。未来映射仅作设计预留：

- `NO_ACTION`：不变；
- `RETRY_*`：由 Worker 产生 retry attempt/event，不应伪造完成；
- `WAIT_DEPENDENCY`：未来才考虑新的等待表示，当前模型保持不变；
- `REQUEST_USER_INPUT`：未来可映射为等待用户输入/审批；
- `REQUIRE_RECONCILIATION`：未来暂停写操作并产生对账任务；
- `MANUAL_INTERVENTION`：未来转人工队列；
- `FAIL_FAST`：沿用现有失败传播。

---

## 4. FailurePolicy Rule 设计

### 4.1 输入与输出

建议接口：

```text
FailurePolicy.decide(
    classification: FailureClassification,
    context: FailurePolicyContext,
) -> RecoveryDecision
```

输出建议：

```text
RecoveryDecision {
    category: FailureCategory
    action: RecoveryAction
    max_retry: int
    backoff_strategy: BackoffStrategy | None
    require_reconciliation: bool
    retry_after: datetime | None
    user_message: str
    reason: str
    policy_id: str
    policy_version: str
}
```

`max_retry=0` 表示本策略不允许自动重试；它不是把当前 Execution 直接改成
`FAILED` 的命令。`user_message` 只能解释事实和下一步，不得覆盖真实失败状态为
成功。

### 4.2 规则匹配维度

规则必须基于通用维度，而不是某一个下游错误码：

1. `category`：根因类别；
2. `side_effect_risk`、原始 `side_effect_state`；
3. `request_sent` 三态；
4. `source`/`phase`：调用前、调用中、调用后；
5. `capability`、`tool_name` 和 ToolContract side-effect 元数据；
6. `idempotent`、`supports_reconciliation`；
7. `attempt`、retry budget、deadline；
8. tenant/user policy、用户是否明确请求重试。

`error_code` 只在分类阶段参与别名/证据映射；Policy 不应为
`JAVA_BACKEND_UNAVAILABLE`、`CREATOR_TIMEOUT` 等具体码编写特殊分支。

### 4.3 建议匹配优先级

Policy 应 fail-closed，并按以下顺序评估：

1. `AUTH_FAILURE` / `PERMISSION_DENIED`：拒绝自动重试；
2. `CONTRACT_MISMATCH`、确定性 `INVALID_ARGUMENT`、`BUSINESS_REJECTED`、
   `STATE_CONFLICT`：快速失败或请求用户/人工修正；
3. `SIDE_EFFECT_UNKNOWN` 或风险 `POSSIBLE/UNKNOWN`：先对账，不能直接 retry；
4. `RATE_LIMIT`：仅在 retry-after、预算和 deadline 允许时 backoff；
5. `DEPENDENCY_UNAVAILABLE`/`TIMEOUT`/`NETWORK_ERROR`：只有未发送或明确未开始
   且 Tool 可安全重放时才允许 retry；
6. `UNKNOWN`：快速失败并保留人工诊断路径。

任何证据冲突（例如 `request_sent=False` 但 `side_effect_state=POSSIBLE`）都按
更保守的风险处理，而不是取更乐观的布尔值。

---

## 5. 典型错误恢复策略

### A. `DEPENDENCY_UNAVAILABLE`

例如 Java、Creator、MCP 或数据库暂时不可达。

- 未发出请求且 Tool 无副作用：`RETRY_WITH_BACKOFF` 候选；
- 请求已发、5xx 无法证明未处理：`REQUIRE_RECONCILIATION`；
- 依赖连续失败超过预算：`WAIT_DEPENDENCY` 或 `MANUAL_INTERVENTION`（未来）；
- 不能因为类别“通常可重试”就忽略幂等键和 deadline。

### B. `TIMEOUT`

例如 Creator submit、poll 或 Java read/write timeout。

- connect/write 前确定未发送：可 backoff retry；
- write 已发出、read 超时或 poll deadline：结果可能已经产生，必须先对账；
- 轮询超时不能简单当成“创建失败”，应优先查询 task/artifact/receipt；
- retry 次数必须受 operation deadline 和预算限制。

### C. `CONTRACT_MISMATCH`

例如 MCP 参数 schema、handler 签名或输出模型漂移。

- Tool 输入校验失败且未调用 handler：`FAIL_FAST` 或 `REQUEST_USER_INPUT`，不重试；
- handler/registry/schema 版本不一致：`MANUAL_INTERVENTION`；
- POST_EXECUTION 输出校验失败：下游已调用，风险至少 `POSSIBLE`，先核对结果；
- 这类错误需要修契约或参数绑定，不是临时网络故障。

### D. `AUTH_FAILURE`

例如 API JWT 缺失、invalid audience、过期 token 或下游 401。

- 用户凭证问题：`REQUEST_USER_INPUT`/`REAUTH`；
- 服务凭证或 audience 配置问题：`MANUAL_INTERVENTION`；
- 认证失败发生在业务 Tool 前，通常无副作用；
- 不自动刷新后无限重试，也不能以成功文案覆盖 401/失败事实。

### E. `PERMISSION_DENIED`

例如 execution owner、tenant、Tool permission 或审批权限不满足。

- `FAIL_FAST`，向用户说明资源或权限范围；
- 只有权限变更后由用户显式 Resume/重新发起；
- 不应通过“换最近任务”或降级到 Legacy 绕过 scope；
- 通常无业务副作用，但已发出的请求仍需以真实 side-effect 证据为准。

### F. `SIDE_EFFECT_UNKNOWN`

例如请求已发但连接断开、Java `RESULT_UNKNOWN`、Creator 已提交但轮询丢失、
数据库 commit 后连接断开。

- 首要动作是 `REQUIRE_RECONCILIATION`；
- 对账前禁止同一逻辑操作盲目 retry；
- 对账确认未执行后，才可按新 attempt/幂等键重试；
- 对账确认已执行，则返回已有 artifact/receipt，不重复创建；
- 对账无法确认时进入人工干预路径，不能伪造 `COMPLETED`。

---

## 6. Retry 边界矩阵

### 6.1 总原则

自动 Retry 需要同时满足：

1. 类别属于暂时性错误（`DEPENDENCY_UNAVAILABLE`、`TIMEOUT`、
   `NETWORK_ERROR`、受控 `RATE_LIMIT`）；
2. 请求未发出，或有权威证据证明 side effect 未开始；
3. ToolContract 声明无副作用或支持幂等重放；
4. 仍有 retry budget、deadline 和租户策略许可；
5. 不存在需要用户修正的参数、权限或契约错误。

### 6.2 `request_sent` 与 `side_effect_state` 组合

| `request_sent` | `side_effect_state` | 自动 Retry | 必须做的事 |
|---|---|---:|---|
| `False` | `NONE` | 允许（仅暂时性类别） | 检查预算、幂等能力和 deadline |
| `False` | `NOT_STARTED` | 允许（仅暂时性类别） | 同上 |
| `False` | `POSSIBLE` | 禁止 | 以 side-effect 证据为准，先对账 |
| `False` | `UNKNOWN` | 禁止 | 先 reconciliation；不要相信乐观的 False |
| `False` | `CONFIRMED` | 禁止同一操作重放 | 使用已有回执或补偿流程 |
| `True` | `NONE`/`NOT_STARTED` | 仅在有权威 ledger 证明状态时允许 | 处理证据冲突；默认降级为保守风险 |
| `True` | `POSSIBLE` | 禁止 | 查询 receipt/artifact/幂等状态 |
| `True` | `UNKNOWN` | 禁止 | `REQUIRE_RECONCILIATION` |
| `True` | `CONFIRMED` | 禁止同一操作重放 | 返回已有结果或执行显式后续动作 |
| `None` | `NONE`/`NOT_STARTED` | 只有 state/ledger 明确可靠时允许 | 记录证据来源；否则保守处理 |
| `None` | `POSSIBLE`/`UNKNOWN` | 禁止 | 对账或人工介入 |
| `None` | `CONFIRMED` | 禁止同一操作重放 | 复用确认结果 |

### 6.3 读操作与写操作

即使 `request_sent=True`，无副作用的查询通常可以按网络/超时策略重试；但
`FailurePolicy` 仍应读取 ToolContract 的 operation metadata，避免把有成本、限流
或非幂等的查询当成无限安全。创建草稿、发布、取消发布等写操作默认需要
idempotency key 或 reconciliation 支持。

---

## 7. 未来 Worker 接入点（仅设计）

未来建议的运行链路为：

```text
Tool Failure
    ↓
ExternalAgentFailure
    ↓
FailureClassifier
    ↓
FailurePolicy
    ↓
RecoveryDecision
    ↓
Worker 执行决定（retry / wait / reconcile / input / fail）
```

最小接入位置应在 `CapabilityExecutor` 已返回失败结果、而当前
`ExecutionWorker._execute_one_step` 调用原始 `RecoveryPolicy` 之前：

1. 将 `ExecutionResult` 中的原始 code、request_sent、state 恢复成 ToolResult
   语义；
2. 调用 normalizer 和 classifier；
3. 组装 `FailurePolicyContext`（step、capability、ToolContract、attempt、ledger）；
4. 调用 Policy 得到 `RecoveryDecision`；
5. Worker 只消费 decision，不重新猜测错误类别；
6. 将原始 code 与分类/策略写入结构化 step event（后续阶段）。

Phase 10-B 不做上述接入，当前 Worker、状态管理和 EventStore 均保持不变。

---

## 8. 最小演进路线

| 阶段 | 目标 | 本阶段允许的新增 | 不做 |
|---|---|---|---|
| Phase 10-A | Failure Classification | 类别、风险、分类纯函数 | 不接策略和 Worker |
| Phase 10-B | Failure Policy | PolicyContext、RecoveryAction、RecoveryDecision、规则设计/纯函数测试 | 不执行 retry、等待、对账或状态迁移 |
| Phase 10-C | Worker Integration | 在执行结果与 Worker 恢复判断之间接入 classifier/policy | 不改变 Planner/ToolContract 核心模型 |
| Phase 10-D | Retry Engine | 有界 backoff、预算、幂等重试和 retry events | 不对未知副作用盲重放 |
| Phase 10-E | Reconciliation | 持久化 ledger、receipt 查询、未知结果收敛 | 不用“重试成功”掩盖副作用不确定性 |

---

## 9. 本阶段验收限制

- 只新增本设计文档；不修改 Worker、RuntimeAgentService、Planner、ToolRuntime、
  ExecutionStateManager、MCP ToolContract、Creator Agent 或 Execution 状态模型。
- 不实现 `Retry`、`WAITING_DEPENDENCY`、`Reconciliation` 或自动恢复。
- 不针对 `JAVA_BACKEND_UNAVAILABLE` 写特殊逻辑；所有策略基于类别、证据、工具
  元数据和运行上下文。
- 不运行服务和完整测试；实现阶段应另行增加 Policy 纯函数测试与安全矩阵测试。

