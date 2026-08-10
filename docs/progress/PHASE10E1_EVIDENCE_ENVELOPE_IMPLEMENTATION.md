# Phase 10-E：Execution Evidence Envelope Implementation

## 1. 实施范围

本阶段完成 Phase10-E-1 Step 2～Step 6 的最小承载实现：建立统一 `ExecutionEvidence`，并让它在 ToolRuntime、InvocationResult、ExecutionResult、FailureNormalizer 和 Worker-facing failure boundary 之间传递。

本阶段只保存和传递已观察事实，不处理失败，不改变恢复策略。

明确未实现：

- Retry Engine
- WAITING_DEPENDENCY
- Reconciliation
- 自动恢复
- 新 Execution 状态
- 任何针对 `JAVA_BACKEND_UNAVAILABLE` 的特殊逻辑

未修改 Planner、IntentSpecProvider、TaskProvider、ExecutionStateManager schema、Creator Agent、Java Backend 和前端。

---

## 2. 修改文件

### 2.1 Runtime 实现

| 文件 | 修改内容 |
| --- | --- |
| `packages/assistant_core/greenbook_assistant_core/execution/evidence.py` | 新增 `ExecutionEvidence` 数据模型、请求摘要哈希和 raw payload 合并逻辑 |
| `packages/assistant_core/greenbook_assistant_core/execution/__init__.py` | 导出 `ExecutionEvidence` |
| `packages/assistant_core/greenbook_assistant_core/execution/invocation.py` | `ExecutionResult` 增加可选 `evidence`，成功/失败/pending 结果都可承载 envelope |
| `packages/assistant_core/greenbook_assistant_core/execution/runtime/tool_runtime.py` | `InvocationResult` 增加 evidence；为正常结果、timeout、handler exception、异步结果构造和保留 evidence |
| `packages/assistant_core/greenbook_assistant_core/execution/runtime/ledger.py` | `LedgerEntry` 增加 evidence；支持在生命周期不变的情况下更新证据 |
| `packages/assistant_core/greenbook_assistant_core/execution/failure_decision.py` | `normalize_failure_payload()` 保留 evidence，并把 evidence 传给 FailureNormalizer |
| `packages/contracts/greenbook_contracts/external_agent_failure.py` | FailureNormalizer 优先读取 evidence；`ExternalAgentFailure` 保存序列化 evidence |
| `apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py` | raw MCP adapter 保留 `tool_call_id`，并将 InvocationResult evidence 原样交给 CapabilityExecutor |

### 2.2 测试

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/unit/test_execution_evidence.py` | pre-execution、timeout、handler exception、external failure、success 五类 evidence 传递测试 |

---

## 3. Evidence 数据模型

`ExecutionEvidence` 位于：

```text
greenbook_assistant_core.execution.evidence.ExecutionEvidence
```

字段分为五组：

### 3.1 Identity

```text
execution_id
step_id
invocation_id
tool_call_id
operation_id
```

这些字段不混用：

- `execution_id`：一次 Runtime Execution。
- `step_id`：Execution 中的计划步骤。
- `invocation_id`：一次 ToolRuntime 调用尝试。
- `tool_call_id`：一次 MCP/传输调用。
- `operation_id`：可跨尝试保持稳定的逻辑操作身份；当前不自动生成。

### 3.2 Request Evidence

```text
request_hash
request_time
request_sent: bool | None
```

`request_hash` 从 tool name、capability 和规范化工具参数生成，不保存原始参数。

`request_sent` 保持三态：

```text
False = 明确没有发送
True  = 明确已经发送
None  = 无法判断
```

实现中没有使用 `bool(request_sent)` 进行 evidence 转换。ToolRuntime 自身 timeout 和 handler exception 会明确生成 `request_sent=None`。

### 3.3 Side Effect Evidence

```text
side_effect_state
```

模型复用 Phase9-B 已有 `SideEffectState` 契约。当前 envelope 重点覆盖：

```text
NONE
NOT_STARTED
POSSIBLE
UNKNOWN
```

如果 raw payload 提供了显式状态，优先保留；只有 legacy payload 没有显式状态时，才按现有兼容规则读取 `side_effect_started` 或送达三态。

### 3.4 External Evidence

```text
receipt_id
external_operation_id
resource_refs
```

这些字段只在上游实际提供时填充，不从错误码或资源名称猜测。

### 3.5 Idempotency Evidence

```text
runtime_idempotency_key
external_idempotency_key
```

Runtime key 和外部业务 key 是两个独立字段：

- Runtime key 来自 `ToolInvocationContext`。
- external key 只从 raw evidence/state 的外部 key 读取。
- 实现没有把 Runtime key 自动复制为 external key。

### 3.6 Error / Response Evidence

```text
error_code
raw_error_type
status_code
phase
trace_id
```

ToolRuntime 自身的 timeout/exception 会写入 error type 和 phase；HTTP status 只有在下游结果实际提供时才会保留。

---

## 4. Evidence 数据流变化

### 4.1 实施前

```text
MCP ToolResult
    ↓ 有限字段映射
InvocationResult
    ↓ RuntimeAgentService 再次压缩
ExecutionResult
    ↓ transient failure normalization
ExternalAgentFailure
    ↓
FailureDecision
    ↓
Worker Event
```

主要丢失：`state`、`side_effect_state`、receipt、`tool_call_id`、调用身份和幂等证据。

### 4.2 实施后

```text
ToolInvocationContext
    └─ 创建基础 ExecutionEvidence
         ├─ execution_id / step_id / invocation_id
         ├─ request_hash / request_time
         ├─ request_sent=None
         └─ runtime_idempotency_key
    ↓
ToolRuntime
    ├─ 合并 MCP raw result / state / receipt
    ├─ 保留 timeout/exception 的 UNKNOWN delivery
    └─ LedgerEntry.evidence
    ↓
InvocationResult.evidence
    ↓ RuntimeAgentService adapter 原样传递
ExecutionResult.evidence
    ├─ 成功结果
    ├─ 失败结果
    └─ pending 结果
    ↓
FailureNormalizer 优先读取 evidence
    ↓
ExternalAgentFailure.evidence + 规范化失败字段
    ↓
FailureDecision / Worker
```

本阶段没有改变 Worker 的恢复动作，也没有让 Worker 执行 Retry；只让决策边界获得更多事实。

---

## 5. 当前可以完整传递的字段

### 5.1 Runtime 主路径可以保真的字段

在 `RuntimeAgentService → ToolRuntime → InvocationResult → ExecutionResult` 主路径中，以下字段现在可以由统一 envelope 传递：

- `execution_id`
- `step_id`
- `invocation_id`
- `request_hash`
- `request_time`
- `request_sent=False/True/None`
- `side_effect_state`
- `runtime_idempotency_key`
- `tool_call_id`（RuntimeAgentService raw MCP adapter 生成并注入）
- `error_code`
- `raw_error_type`（ToolRuntime timeout/exception 路径）
- `phase`
- `trace_id`（raw result 提供时）
- `receipt_id`（raw result 提供时）
- `external_operation_id`（raw result/evidence 提供时）
- `resource_refs`（raw result 提供时）
- `external_idempotency_key`（raw result/state 提供时）

### 5.2 FailureNormalizer 现在可以保真的字段

当 `ExecutionEvidence` 进入 `normalize_failure_payload()` 或直接传给 `normalize_external_failure()` 时，FailureNormalizer 优先使用：

- evidence 中的 `error_code`；
- evidence 中的 `request_sent`，包括 `None`；
- evidence 中的 `side_effect_state`；
- evidence 中的 `phase`、`trace_id`、`receipt_id`；
- evidence 中的 `external_idempotency_key`；
- 完整序列化 evidence 存入 `ExternalAgentFailure.evidence` 和 metadata。

因此 evidence 中的 `request_sent=None` 不会再因为 normalizer 输入适配而变成 `False`。

---

## 6. 仍然缺失或有边界的能力

本阶段是最小承载实现，以下能力仍然明确未完成：

1. `operation_id` 当前只是可选承载字段，Runtime 不负责自动生成跨 retry 的逻辑操作 ID。
2. `external_operation_id`、receipt 和 resource refs 只有在 MCP/Creator/Java raw result 提供时才能完整保存；本阶段没有修改外部服务契约。
3. Java/Creator 客户端仍未统一返回 status code、connect/write/read timeout 阶段和服务端操作状态。
4. Runtime key 与 external key 已分字段，但当前没有持久化映射，也没有验证 external key 的 TTL。
5. `ToolExecutionLedger` 仍是单次 Runtime 生命周期内的内存账本，不支持重启、跨进程或长期查询。
6. `ExecutionEvent.STEP_FAILED` 仍只记录现有错误/决策摘要；本阶段没有修改 Event payload 或 ExecutionStateManager schema。
7. `ExternalAgentFailure.evidence` 当前以序列化 mapping 保存，以避免 contracts 包反向依赖 assistant-core；它还不是持久化 evidence store。
8. `status_code` 只有在 raw evidence 已提供时保留，Runtime 不会从错误码推断 HTTP 状态。
9. 组合工具的 operation-level evidence 仍依赖上游提供 `operation_id` 和子操作信息；本阶段没有实现组合操作追踪。

这些缺口是后续 External Operation Tracking 和外部契约工作的范围，不在本阶段通过猜测补齐。

---

## 7. 向后兼容性

- `ExecutionResult.ok`、`data`、`error_code`、`retryable` 等旧字段保留。
- `ExecutionResult.evidence` 和 `InvocationResult.evidence` 都是 optional。
- 旧的 raw handler 可以继续返回原有字典。
- 没有 evidence 的旧结果会获得 Runtime 基础 evidence；无法从旧结果推断的字段保持为空或 UNKNOWN。
- 没有修改 ExecutionStateManager、PlanStep、Worker 生命周期或 Retry 行为。
- 没有删除或重命名 `request_sent`、`side_effect_state`、`receipt_id` 等已有字段。

---

## 8. 测试结果

### 8.1 Evidence 与 Runtime 相关测试

```text
python -m pytest \
  tests/unit/test_execution_evidence.py \
  tests/unit/test_tool_runtime.py \
  tests/unit/test_external_agent_failure.py \
  tests/unit/test_failure_decision.py \
  tests/unit/test_capability_executor.py \
  tests/unit/test_execution_worker.py \
  tests/unit/test_runtime_agent_service.py
```

结果：

```text
68 passed
```

覆盖：

- pre-execution：`False + NONE` 保持不变；
- timeout：`None + UNKNOWN` 保持不变；
- handler exception：`None + UNKNOWN` 保持不变；
- external failure：`True + POSSIBLE + receipt` 完整保留；
- success：operation、receipt、runtime/external idempotency key 完整传递。

### 8.2 兼容性测试

```text
python -m pytest \
  tests/unit/test_tool_result.py \
  tests/unit/test_contracts.py \
  tests/integration/test_assistant_runtime_contracts.py
```

结果：

```text
41 passed
```

测试过程中只有既有环境警告：pytest cache 目录权限警告，以及 Starlette/httpx deprecation warning；没有失败。

当前环境未安装 `ruff`，因此未能执行 Ruff lint；本次相关 Python 文件已通过上述测试导入和执行。

---

## 9. 阶段结论

Phase10-E 已完成最小 Execution Evidence 承载能力：

```text
Tool Failure
    ↓
ExecutionEvidence
    ↓
InvocationResult
    ↓
ExecutionResult
    ↓
ExternalAgentFailure
    ↓
FailureDecision / Worker
```

当前 Runtime 已能够保存和传递比错误码更完整的事实，尤其不会在 ToolRuntime timeout、handler exception 和 evidence normalization 路径中把未知送达状态改成 `False`。

本阶段到此停止，不进入 Retry Engine。
