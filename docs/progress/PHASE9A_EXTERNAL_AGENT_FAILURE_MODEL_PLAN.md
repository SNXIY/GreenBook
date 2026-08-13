# Phase 9-A：External Agent Failure Model 设计

状态：只读分析与设计，未修改运行代码。

审计输入：`execution_id=70e41538-fe59-47fc-bb10-fd6d237170ef`，请求“写一篇 Redis 缓存三大机制文章”，失败码 `JAVA_BACKEND_UNAVAILABLE`。

本设计不针对 Creator 单一案例，而是为所有 Runtime 下游 Agent、MCP、模型和 Java Facade 依赖提供统一语义。Phase 9-A 不改变现有状态模型；文中的新增状态、事件和持久化字段属于后续实现边界。

## 1. 当前调用链与职责边界

```text
RuntimeAgentService
  → CapabilityExecutor
    → ToolRuntime
      → MCP Server
        → content.create_draft
          → CreatorClient
            → Creator Agent
          → JavaClient
            → Java Agent Facade
```

| 层 | 当前职责 | 当前错误/恢复行为 | 主要问题 |
|---|---|---|---|
| `RuntimeAgentService._execute_single` | 组装 Planner、ArgumentBinder、Worker、ToolRuntime；在结束时生成 `RuntimeResult` | 把失败步骤映射到 `RuntimeResult.FAILED` | 不拥有统一下游失败语义；每次执行创建局部 ledger |
| `CapabilityExecutor` | capability→tool 选择、参数绑定、`InvocationResult`→`ExecutionResult` | 保留 `code`、`retryable`、`request_sent` | 不根据 ToolContract 的 retry policy 决策 |
| `ToolRuntime` | 单次调用超时、trace、幂等检查、ledger 记录 | 120 秒 `wait_for`；异常/超时记录 FAILED/TIMEOUT | 超时只知道调用超时，不知道外部写入是否已发生 |
| MCP Server | input schema、handler signature、output schema 校验 | 前置校验失败时 `downstream_called=false`；handler 输出按 `ToolResult` 校验 | 没有统一外部依赖失败分类 |
| `content.create_draft` | Creator 生成文稿，随后 Java 创建 draft 并 GET 验证 | Creator/Java 失败结果多数直接向上返回；Java GET 验证失败被包装成 `INTERNAL_ERROR` | 一个工具内含两个有副作用的外部阶段，缺少阶段级 receipt/ledger |
| `CreatorClient` | POST Creator task；轮询 GET 直到 240 秒；GET artifact | 连接失败→`CREATOR_UNAVAILABLE`；轮询 deadline→`TIMEOUT` | 轮询超时后没有统一“查询既有 task 再恢复”状态 |
| `JavaClient` | HTTP connect/read/write/pool timeout、HTTP 错误映射 | 连接/网络/5xx→`JAVA_BACKEND_UNAVAILABLE`；写 ReadTimeout→`RESULT_UNKNOWN` | 5xx 当前也默认 `request_sent=false`，无法表达“已收到响应但可能已有副作用” |
| `ExecutionWorker` | Step 结果、StepExecution、下游跳过、Execution 终态 | 用 `RecoveryPolicy` 错误码白名单决定 retry | 不读取 ToolContract retry policy，也不读取 `InvocationResult.retryable` |
| `ExecutionEventStore` | 内存 append-only 生命周期事件 | `/events`、SSE 从此处读取 | 没有 `DEPENDENCY_*`、reconciliation、side-effect receipt 事件 |

### 1.1 当前请求的实际生命周期

`content.create_draft` 的顺序固定为：

1. 生成一个 Creator/Java 共用的业务幂等 key；
2. `CreatorClient.create_task`：`POST /api/v1/creator/tasks`；
3. `wait_for_completion`：轮询 `GET /api/v1/creator/tasks/{task_id}`；
4. `get_artifact`：`GET /api/v1/creator/tasks/{task_id}/artifacts/{artifact_id}`；
5. `JavaClient.create_draft`：`POST /api/v1/agent/drafts`；
6. `JavaClient.get_draft`：`GET /api/v1/agent/drafts/{draft_id}` 验证；
7. 成功才更新 `SessionContext.active_draft_id` 和 Runtime artifact。

因此，当前用户文案对应 Java handoff 阶段，而不是 MCP 参数校验或 Creator 输入阶段。Creator 生成成功但 Java 创建失败时，Creator task/artifact 可能已经存在，Java draft 可能不存在。

### 1.2 超时边界

当前有三层时间限制：

- JavaClient：connect 5 秒、read 30 秒、write 30 秒、pool 5 秒；
- CreatorClient：HTTP client 默认 240 秒，完成轮询 deadline 240 秒；
- ToolRuntime：`CapabilityExecutor` 为每个工具调用设置 120 秒外层 `wait_for`。

同步 `content.create_draft` 被 ToolRuntime 包住时，外层 120 秒可能先于 Creator 的 240 秒 deadline 到期。超时只会得到 `TIMEOUT`，不会自动判断 Creator task 是否仍在后台完成。该嵌套 timeout 是通用长任务风险，不是 Creator 专属修复点。

### 1.3 错误转换与恢复归属

```text
httpx exception / HTTP status
  → JavaClient/CreatorClient ToolResult
  → content.create_draft 直接返回（或少数分支包装）
  → MCP output schema
  → ToolRuntime InvocationResult
  → CapabilityExecutor ExecutionResult
  → Worker RecoveryPolicy
  → StepExecution / PlanExecution
  → RuntimeResult / Assistant response
```

当前职责分散：

- 下游客户端负责“翻译错误”；
- ToolRuntime 负责“单次 timeout/ledger”；
- Worker/RetryManager 负责“是否重试”；
- 没有一层负责统一的降级或 dependency wait；
- `RuntimeResult.fallback_allowed` 只是结果字段，不是实际降级执行器。

## 2. 当前错误处理体系审计

### 2.1 ToolResult 现状

`packages/contracts/greenbook_contracts/tool_result.py` 已有 `code`、`retryable`、`request_sent`、`state`、`trace_id`、`receipt_id`，并提供：

- `JAVA_BACKEND_UNAVAILABLE`：`retryable=true`、默认 `request_sent=false`；
- `CREATOR_UNAVAILABLE`：`retryable=true`、默认 `request_sent=false`；
- `TIMEOUT`：`retryable=true`、`request_sent=true`；
- `RESULT_UNKNOWN`：`retryable=false`、`request_sent=true`；
- `REQUEST_NOT_SENT`：`retryable=true`、`request_sent=false`。

该字段集合足以承载基础错误，但没有表达：依赖名称、失败阶段、是否需要查询收据、下一次重试时间、side effect 估计状态和恢复动作。

### 2.2 Execution FAILED 转换

`ExecutionWorker._execute_one_step` 当前只调用：

```text
RecoveryPolicy.can_retry_failure(step, result.error_code)
```

若错误码不在默认集合，Worker 将步骤置为永久 `FAILED`，`ExecutionStateManager._update_execution_status` 将 Execution 置为 `FAILED`，再由 scheduler 将下游步骤置为 `SKIPPED`。

当前默认 retryable code 只有：

```text
TIMEOUT
NETWORK_ERROR
RATE_LIMIT
TEMPORARY_UNAVAILABLE
```

所以 `JAVA_BACKEND_UNAVAILABLE` 和 `CREATOR_UNAVAILABLE` 即使在 `ToolResult` 与 Phase 8 ToolContract metadata 中标记为可重试，也会立即失败。`FAILED_RETRYABLE` 只能在 Worker 采用该白名单时产生；它不是一个真正的“等待依赖”状态。

### 2.3 ToolContract RetryPolicy

`content.create_draft` 的 Phase 8 contract 包含：

```text
has_side_effect=true
idempotent=true
external_systems=(creator, java)
max_attempts=2
retryable_error_codes 包含 CREATOR_UNAVAILABLE、JAVA_BACKEND_UNAVAILABLE 等瞬态错误
```

但是全仓库搜索显示 Worker、RetryManager 当前直接构造默认 `RecoveryPolicy`，没有消费 MCP 导出的 `RetryPolicy`。当前存在两个权威来源，导致契约与执行恢复决策漂移。后续应统一为“ToolContract 提供默认策略，Runtime 根据 request_sent/side-effect 状态做安全裁剪”。

### 2.4 Side Effect Ledger

`ToolExecutionLedger` 当前：

- 每次 `RuntimeAgentService._execute_single` 新建一个局部实例；
- `record_start`→`record_complete`/`record_failure`/`record_timeout`；
- 只对 `COMPLETED` 结果 `try_replay`；
- `FAILED`/`TIMEOUT` 不 replay；
- 只有内存查询 `list_by_execution`，没有持久化或 HTTP API。

这对纯读工具可以接受，但对“请求可能已到达、响应丢失”的写工具不安全：一次 `FAILED`/`TIMEOUT` 会允许下一次调用再次真正写入。

### 2.5 EventStore

`ExecutionEventStore` 是进程内 append-only 存储，已有 `STEP_FAILED`、`STEP_RETRY_*`、`EXECUTION_FAILED` 等事件，但没有：

- `DEPENDENCY_UNAVAILABLE`；
- `DEPENDENCY_RETRY_SCHEDULED` / `DEPENDENCY_RETRY_STARTED`；
- `RESULT_RECONCILIATION_REQUIRED`；
- `SIDE_EFFECT_CONFIRMED` / `SIDE_EFFECT_NOT_FOUND`；
- 外部 receipt、idempotency key 和 request_sent 的标准 payload。

SSE 只是轮询该 EventStore；若 Execution 已被标记 FAILED，前端无法区分“永久失败”和“等待依赖恢复”。

## 3. JAVA_BACKEND_UNAVAILABLE 的决策：A、B 还是 C

推荐：**以 C（`WAITING_DEPENDENCY`）为语义主状态，内部可执行有界 retry；不能把所有下游失败简单归为 B。**

针对本次错误，先看 request/side-effect 证据：

| 条件 | 推荐决策 | 原因 |
|---|---|---|
| `request_sent=false`，且依赖错误被判定为瞬态 | `WAITING_DEPENDENCY` → 有界自动 retry | 没有确认写入，可用同一幂等 key 安全重试 |
| `request_sent=true`，但有明确 receipt/成功响应 | 先查询/确认资源，不重新 POST | 不能重复创建；进入恢复查询而非盲重试 |
| `request_sent=true`，没有响应或为写 ReadTimeout | `RESULT_UNKNOWN`/`RECONCILIATION_REQUIRED` | 先按幂等 key 查询；确认不存在后才 retry |
| 认证失败、权限拒绝、参数校验、业务拒绝 | A：立即 `FAILED` | 自动 retry 不会改变确定性错误 |
| Rate limit / 临时不可用 | C：按 `Retry-After` 进入等待 | 需要退避，避免雪崩 |

因此，当前 `JAVA_BACKEND_UNAVAILABLE` 不应无条件立即 FAILED，也不应无条件立即重试。应由 `request_sent`、side-effect metadata、receipt 和尝试次数共同决定。达到最大尝试次数后才转为 `FAILED`，并保留 `error_code`、`dependency` 和恢复建议。

## 4. 通用 ExternalAgentFailure 模型

建议在共享 contracts 层定义一个只描述事实与恢复建议的模型；它不执行 retry，也不直接改变 PlanExecution：

```text
ExternalAgentFailure {
    dependency: str                 # java | creator | mcp | model | community ...
    error_code: str                 # JAVA_BACKEND_UNAVAILABLE ...
    retryable: bool
    user_visible_message: str
    recovery_action: str             # RETRY | WAIT | RECONCILE | REAUTH | USER_ACTION

    # 建议的安全扩展字段
    phase: str                      # CONNECT | SUBMIT | POLL | READ | VERIFY | MODEL
    request_sent: bool | None       # None=未知，不得当作 false
    side_effect_state: str          # NONE | NOT_STARTED | POSSIBLE | CONFIRMED | UNKNOWN
    idempotency_key: str
    receipt_id: str | None
    retry_after_seconds: float | None
    attempt: int
    max_attempts: int
    cause_code: str | None
    trace_id: str | None
}
```

`request_sent` 与 `side_effect_state` 必须三态/多态处理，不能使用默认 `false` 掩盖未知写入状态。`user_visible_message` 只供 Presenter 使用，不能覆盖 Execution 的事实状态。

### 4.1 错误码映射建议

| 输入错误 | `dependency` | `retryable` | `side_effect_state` 默认判断 | `recovery_action` | 终态策略 |
|---|---|---:|---|---|---|
| `JAVA_BACKEND_UNAVAILABLE` | `java` | 是（仅瞬态） | `NOT_STARTED` 或 `UNKNOWN`，取决于 request_sent | `WAIT`/`RETRY`；未知时 `RECONCILE` | 有界 retry 后 `FAILED` |
| `CREATOR_TIMEOUT` / Creator `TIMEOUT` | `creator` | 是 | `POSSIBLE`（task 可能仍运行） | `QUERY_TASK_THEN_RETRY` | task 终态未知时保持等待 |
| `MCP_TIMEOUT` / ToolRuntime `TIMEOUT` | `mcp` | 条件式 | 读工具 `NONE`；写工具 `UNKNOWN` | `RECONCILE` 或 `RETRY` | 证据不足时不盲写 |
| `MODEL_TIMEOUT` | `model` | 是（无副作用时） | `NONE` 或 Creator task `POSSIBLE` | `RETRY_SAME_TASK` | 超限后 `FAILED` |
| `RATE_LIMIT` | 被限流依赖 | 是 | 沿用原 side-effect 状态 | `WAIT_RETRY_AFTER` | 退避耗尽后 `FAILED` |
| `AUTH_FAILURE` | 具体依赖 | 否（除非刷新 token 已知可行） | 通常 `NOT_STARTED`，写响应未知时仍需 reconcile | `REAUTH`/`USER_ACTION` | 立即 `FAILED` 或等待重新认证 |

映射器应保留原始 `ToolResult.code`，不要把所有错误折叠成 `DEPENDENCY_UNAVAILABLE`；通用 code 只用于策略分类，原码用于审计和用户诊断。

## 5. Runtime 状态流转设计

### 5.1 推荐的显式状态机

```text
RUNNING
  │
  ├─ 外部依赖瞬态失败、request_sent=false
  │       ↓
  │  WAITING_DEPENDENCY
  │       │  等待 retry_after / health / callback
  │       ↓
  │  RETRYING（逻辑阶段/事件，不一定是永久 ExecutionStatus）
  │       ↓
  │  RUNNING ───────────────→ COMPLETED
  │       │
  │       └─ 次数耗尽/不可恢复 ─→ FAILED
  │
  └─ 写请求响应未知
          ↓
     RECONCILIATION_REQUIRED
          ├─ 查询到已创建资源 → COMPLETED/继续下游
          ├─ 确认未创建 → RETRYING（使用原幂等 key）
          └─ 仍无法确认 → 保持等待或人工介入
```

### 5.2 与当前模型的关系

当前 `ExecutionStatus` 只有 `PENDING/RUNNING/PAUSED/WAITING_APPROVAL/WAITING_HUMAN/COMPLETED/FAILED/CANCELLED`，没有 `WAITING_DEPENDENCY`；当前 `StepStatus.FAILED_RETRYABLE` 又会让 `_update_execution_status` 把 Execution 置为 `FAILED`。因此不建议把 `WAITING_DEPENDENCY` 偷换成 `WAITING_HUMAN` 或继续伪装为普通 `FAILED`。

后续实现应选择一种向后兼容的方式：

1. 为 Execution/Step 增加明确的 dependency-wait 状态，并让 API/SSE 可展示 retry_at、dependency 和 recovery_action；或
2. 在不扩展枚举的过渡阶段，保留 `RUNNING` + 标准化 dependency-wait event/checkpoint，由独立恢复调度器唤醒；不能让 `FAILED_RETRYABLE` 终止 SSE 后再依赖人工猜测。

本 Phase 只确定语义，不选择性修改状态模型。

### 5.3 事件建议

在现有 EventType 体系上增加或等价投影以下事件：

- `DEPENDENCY_UNAVAILABLE`：payload 包含 ExternalAgentFailure；
- `DEPENDENCY_RETRY_SCHEDULED`：attempt、retry_at、backoff；
- `DEPENDENCY_RETRY_STARTED`；
- `DEPENDENCY_RECOVERED`；
- `RESULT_RECONCILIATION_REQUIRED`；
- `SIDE_EFFECT_CONFIRMED` / `SIDE_EFFECT_NOT_FOUND`。

事件必须带 `execution_id`、`step_id`、`dependency`、`error_code`、`request_sent`、`side_effect_state`、`idempotency_key`（脱敏或不可逆摘要）和 `trace_id`。用户展示层据此显示“等待依赖恢复/正在确认结果”，而不是错误地显示“已完成”。

## 6. Side Effect 风险与幂等设计

### 6.1 重试是否会产生重复文章

会，当前设计存在风险：

1. Creator task 可能已创建并完成；
2. Java POST 可能已到达并创建 draft，但响应在网络中丢失；
3. JavaClient 可能返回 `RESULT_UNKNOWN`，或在部分异常路径被归类为 `JAVA_BACKEND_UNAVAILABLE`；
4. 当前 ledger 对 `FAILED/TIMEOUT` 不 replay，且本次 ledger 不持久化；
5. 如果下一次 retry 重新发起 POST，可能出现重复文章。

`content.create_draft` 当前把一个业务 key 传给 Creator 和 Java，这比每次生成随机 key 更安全，但 key 的生成逻辑位于 `ToolContext.idempotency_key`，基于 conversation + operation + content scope；它没有把 task/step 作为一等持久化实体，跨任务同文案可能发生意外碰撞。

### 6.2 推荐的两层幂等 key

```text
runtime_invocation_key = hash(execution_id, step_id, tool_name)
external_operation_key = hash(tenant_id, task_id, logical_step_id,
                              operation_version, canonical_arguments)
```

- Runtime invocation key：用于本次 Execution 的 ToolRuntime ledger；同一个 step 的恢复应复用，而不是每次 retry 生成新 key。
- External operation key：用于 Creator/Java 等下游写接口；必须在首次尝试时生成并持久化到 step checkpoint/ledger，重启和 retry 继续复用。
- 若用户确实创建了一个全新 Task，即使内容相同，也应有新的 `task_id`/logical operation，避免与历史请求错误去重。

用户给出的 `execution_id + step_id` 可以作为 Runtime 层 key；对于跨 Execution 恢复或外部下游，必须再有稳定的 logical operation id，不能只依赖可能变化的 execution id。

### 6.3 Unknown 结果的安全规则

```text
request_sent=false  → 可以用同一 external_operation_key 重试
request_sent=true + receipt_id  → 查询/确认 receipt，不重新 POST
request_sent=true + 无 receipt → RECONCILE；确认不存在后才重试
request_sent=None  → 按 UNKNOWN 处理，禁止盲重试
```

成功确认后，ledger 应缓存最终资源引用（draft_id、creator_task_id、artifact_id），后续恢复直接 replay/继续下游；失败和超时不能简单清除 key。

## 7. 通用恢复策略

建议恢复决策输入按以下优先级计算：

```text
明确业务/权限/参数错误
  > request_sent + side_effect evidence
  > ToolContract retry policy
  > retry_after / backoff
  > 最大尝试次数
```

建议策略：

1. 先由 ExternalAgentFailureNormalizer 统一错误事实；
2. 由 RecoveryDecider 判断 `RETRY_NOW`、`WAIT_DEPENDENCY`、`RECONCILE`、`FAIL`、`REAUTH`；
3. 由恢复调度器执行重试，不由 ToolRuntime 在未知写结果时直接重放；
4. 每次尝试写入 EventStore 和持久化 ledger；
5. 达到上限后才让 Step/Execution 进入 `FAILED`，并保留最后一次外部证据。

ToolContract 的 `max_attempts` 应作为默认上限，但可被更严格的 side-effect/unknown 规则覆盖；不能被 Worker 的另一套硬编码白名单静默覆盖。

## 8. 后续实现边界（本阶段不执行）

后续阶段需要单独设计/实现：

- ExternalAgentFailure contract/normalizer；
- ToolContract retry policy 与 RecoveryPolicy 的单一来源接线；
- dependency-wait/reconciliation 状态或等价 checkpoint；
- 持久化 SideEffect Ledger 和查询 API；
- Java/Creator receipt、按幂等 key 查询接口；
- EventStore/SSE 的 dependency/reconciliation 事件；
- `content.create_draft` 的 Creator task 与 Java draft 分阶段 ledger/补偿语义；
- 针对连接失败、5xx、写 ReadTimeout、Creator 超时、限流、认证失败的 unit/E2E 测试。

本阶段明确不修改：`RuntimeAgentService`、`Planner`、`Worker`、`ToolRuntime`、MCP handler、Creator Agent 代码以及任何数据库/迁移。

## 9. 设计验收标准

后续实现完成后，至少应验证：

1. `JAVA_BACKEND_UNAVAILABLE(request_sent=false)` 进入等待并按相同 key 有界重试，而非立即伪装为永久失败；
2. Java 写 ReadTimeout/`RESULT_UNKNOWN` 不会盲目创建第二篇文章；
3. Creator 超时会查询既有 task，不会无条件创建第二个 Creator task；
4. `AUTH_FAILURE`、参数错误和业务拒绝立即失败且不会重试；
5. 前端能区分 `WAITING_DEPENDENCY`、`RECONCILIATION_REQUIRED`、`RETRYING` 和 `FAILED`；
6. Execution API、SSE、EventStore、ledger 和最终 RuntimeResult 使用同一 `error_code`、`dependency` 和 recovery action；
7. 进程重启后仍能依据持久化 idempotency/receipt 恢复，而不是丢失本次副作用事实。

