# Phase 9-B：ExternalAgentFailure Contract

## 目标

在不接入 Runtime 执行生命周期的前提下，为下游 Agent/MCP/模型失败建立共享 contracts 层事实模型和纯函数 normalizer。

本阶段只做：

- `ToolResult` → `ExternalAgentFailure`；
- 保留原始 `error_code`；
- 保留 `request_sent` 的三态语义；
- 保留/推导 side-effect 状态；
- 给出后续恢复动作建议。

本阶段不做 retry、不改变 Execution/Step 状态、不写 EventStore、不调用任何下游服务。

## 修改文件

| 文件 | 变更 |
|---|---|
| `packages/contracts/greenbook_contracts/external_agent_failure.py` | 新增 `ExternalAgentFailure`、`SideEffectState`、`RecoveryAction`、`FailureNormalizer` 和纯函数 `normalize_external_failure`。 |
| `packages/contracts/greenbook_contracts/tool_result.py` | `request_sent` 类型扩展为 `bool \| None`；默认仍是 `False`，显式 `None` 表示无法确认请求是否发出。 |
| `packages/contracts/greenbook_contracts/__init__.py` | 导出新 contracts API。 |
| `tests/unit/test_external_agent_failure.py` | 新增纯函数/模型单元测试。 |

未修改：`RuntimeAgentService`、`Planner`、`Worker`、`ToolRuntime`、MCP handler、Creator Agent、Execution 状态模型和数据库。

## ExternalAgentFailure 模型

核心字段：

```text
dependency             java | creator | mcp | model | identity | ...
error_code             原始 ToolResult.code，不改写
retryable              规范化后的可恢复提示
user_visible_message   ToolResult.user_message 优先
recovery_action        RETRY | WAIT_DEPENDENCY | RECONCILE | REAUTH | FAIL
request_sent           True | False | None
side_effect_state      NONE | NOT_STARTED | POSSIBLE | UNKNOWN | CONFIRMED
```

同时携带 `message`、`phase`、`trace_id`、`receipt_id`、`idempotency_key` 和原始 `state` metadata，供后续 Runtime 恢复层使用。

### request_sent 三态

- `False`：确认未发送，可考虑使用相同幂等 key 安全重试；
- `True`：请求已发送，不能仅凭错误码重放写操作；
- `None`：无法确认，必须按未知副作用处理。

`ToolResult` 的默认值仍为 `False`，避免破坏已有调用方；只有确实无法判断时才传入 `None`。

### side_effect_state

Normalizer 优先读取 `ToolResult.state["side_effect_state"]`，其次读取 `side_effect_started`，最后根据 `request_sent` 推导：

| 输入证据 | 推导 |
|---|---|
| 显式 `NONE` / `NOT_STARTED` / `POSSIBLE` / `UNKNOWN` | 原样保留 |
| `side_effect_started=True` | `POSSIBLE` |
| `side_effect_started=False` | `NOT_STARTED` |
| 没有 state 且 `request_sent=True` | `POSSIBLE` |
| 没有 state 且 `request_sent=False` | `NOT_STARTED` |
| 没有 state 且 `request_sent=None` | `UNKNOWN` |

### 支持的错误码

Normalizer 支持以下 Phase 9-A 错误族，并保留传入的原始大小写和值：

- `JAVA_BACKEND_UNAVAILABLE`
- `CREATOR_TIMEOUT` / `CREATOR_UNAVAILABLE`
- `MCP_TIMEOUT`
- `MODEL_TIMEOUT`
- `RATE_LIMIT`
- `AUTH_FAILURE` 及现有认证别名 `AUTHENTICATION_FAILED`、`AUTHENTICATION_REQUIRED`、`UNAUTHORIZED`

另支持 `TOO_MANY_REQUESTS`、`LLM_TIMEOUT` 别名；未登记 code 仍可作为通用外部失败保留，dependency 默认为 `unknown`。

## 规范化 recovery_action

Normalizer 只提出建议，不执行动作：

| 条件 | 建议 |
|---|---|
| 认证失败 | `REAUTH`，并强制 `retryable=False` |
| 限流 | `WAIT_DEPENDENCY` |
| side effect 为 `POSSIBLE`、`UNKNOWN` 或 `CONFIRMED` | `RECONCILE`，禁止盲重试 |
| retryable 且副作用为 `NONE/NOT_STARTED` | `RETRY` |
| 其他不可恢复错误 | `FAIL` |

该建议不等同于当前 Worker 的执行状态；后续 Phase 才接入 bounded retry、dependency wait 和 reconciliation。

## 纯函数边界

```text
ToolResult
  → normalize_external_failure(...)
  → ExternalAgentFailure
```

Normalizer：

- 不读取 ExecutionRepository；
- 不写 ExecutionEventStore；
- 不调用 ToolRuntime/MCP/Creator/Java；
- 不生成新的 retry request；
- 不覆盖原始 `error_code`；
- 对成功 `ToolResult` 抛出 `ValueError`，避免把成功误报成失败。

## 测试覆盖

`tests/unit/test_external_agent_failure.py` 覆盖：

1. 六类错误码：Java、Creator、MCP、Model、Rate Limit、Auth；
2. `request_sent=False`、`True`、`None`；
3. `side_effect_state=NONE/NOT_STARTED/POSSIBLE/UNKNOWN`；
4. 原始 error code、trace metadata 和输入对象不被修改；
5. 认证失败不会被不安全的 `retryable=True` hint 推动重试；
6. 成功 ToolResult 被拒绝规范化。

## 当前限制与下一阶段

本阶段没有让 Worker 消费 `ExternalAgentFailure`，所以当前 `JAVA_BACKEND_UNAVAILABLE` 仍不会自动进入 `WAITING_DEPENDENCY`。后续接入必须另行处理：

- ToolContract RetryPolicy 与 RecoveryPolicy 的单一来源；
- dependency-wait/reconciliation 状态和事件；
- 持久化 side-effect ledger；
- 以 logical operation id 稳定外部幂等 key；
- Java/Creator receipt 查询和未知写入恢复。

