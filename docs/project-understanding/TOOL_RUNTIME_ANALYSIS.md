# GreenBook Tool Runtime Analysis

## 1. 定位

Tool Runtime 是 Agent 调用外部能力的**统一执行层**。它分为三个层次：

- **Tool Metadata** (在 `packages/contracts`) — 共享的 Tool 描述、策略、结果契约
- **Tool Policy** (在 `packages/agent_core/toolruntime/`) — 执行前的策略检查
- **Tool Execution** (在 `packages/agent_core/execution/runtime/`) — 实际调用 + 幂等 + 超时

---

## 2. 核心概念

### ToolContract (契约)

```python
ToolContract:
  name: str                    # "content.create_draft"
  category: str                # "content"
  capability: str              # "GENERATE_CONTENT"
  description: str             # LLM 可读的描述
  handler: Callable            # async def handler(ctx, **kwargs) -> ToolResult
  input_schema: BaseModel      # Pydantic 入参模型
  output_schema: ToolResult    # 统一输出信封
  operations: list[str]        # 业务操作列表
  policy: ToolPolicyMetadata   # 策略元数据
```

### ToolMetadata (元数据 - LLM 可见)

```python
ToolMetadata:
  name: str
  description: str             # LLM 选择 tool 的依据
  capabilities: list[str]      # 能力标签
  input_schema: dict           # OpenAI function-calling schema
  output_schema: dict
  provider: str                # "mcp"
  tags: list[str]
  policy: ToolPolicyMetadata
```

### ToolPolicyMetadata (策略)

```python
ToolPolicyMetadata:
  risk_level: READ | IDEMPOTENT_WRITE | DESTRUCTIVE_WRITE
  requires_approval: bool
  permission: PermissionPolicy
  side_effect: SideEffectMetadata
  retry_policy: RetryPolicy
  cost: int                    # 相对成本
  timeout_seconds: int
```

### ToolResult (统一结果信封)

```python
ToolResult:
  ok: bool
  code: ErrorCode
  message: str                 # 技术错误信息
  user_message: str            # 用户可见中文信息
  retryable: bool
  request_sent: bool | None    # None = 不确定 (写操作 key)
  state: SideEffectState       # NONE|NOT_STARTED|POSSIBLE|UNKNOWN|CONFIRMED
  data: Any                    # 业务数据
  trace_id: str
  receipt_id: str              # Java/Creator 返回的凭证
  resource_refs: list          # 创建的资源引用
```

---

## 3. ToolRegistry (注册与查询)

```
TOOL_POLICY_CATALOG (contracts/tool_contract.py)
  └─ 15 个 tool 的策略定义 (唯一权威)
       │
       ├─ MCP tool_registry.py 注册 handler (16 个)
       │   └─ 每个 handler 绑定到对应的 ToolContract
       │
       ├─ ToolRegistry (metadata 投影, 无执行能力)
       │   └─ 提供 LLM 可见的 tool 描述
       │
       └─ SecurityPolicy 读取 risk_level + requires_approval
```

**分开注册的原因**:
- `TOOL_POLICY_CATALOG` — 静态策略 (何时需要审批？风险等级？可重试？)
- MCP `tool_registry` — 执行 handler (怎么调用？)
- `ToolRegistry` — 元数据投影 (LLM 看到什么？)

防止策略和执行耦合，避免一个 tool 有两个不同的风险定义。

---

## 4. ToolPolicyGate (策略门)

```
ToolPolicyGate.evaluate(tool_name):
  │
  ├─ 读取 ToolPolicyMetadata
  │
  ├─ 检查权限 scope
  │   不足 → DENY
  │
  ├─ 检查 requires_approval
  │   需要审批且未审批 → WAITING_HUMAN
  │
  ├─ 检查 side_effect + cost
  │   multi_step / long_running / side_effect / destructive / retry>1 / timeout>120s
  │   → QUEUE (异步执行)
  │
  └─ 否则 → SYNC (同步执行)
```

**未知 tool → DENY (fail-closed)**。

---

## 5. ToolRuntime (执行运行时)

```
ToolRuntime.invoke(invocation_context):
  │
  ├─ 1. 计算 idempotency_key
  │     invoke:{tool_name}:{sha256(conversation + tool_name + args)}
  │
  ├─ 2. 检查 Ledger (幂等)
  │     ├─ COMPLETED → replay result
  │     ├─ IN_PROGRESS → wait (async)
  │     └─ 无记录 → 继续
  │
  ├─ 3. ledger.record_start(key)
  │
  ├─ 4. 调用 raw_handler(tool_name, tool_args)
  │      ├─ 超时 → asyncio.wait_for(timeout)
  │      └─ async 工具 → AsyncTaskHandle (后台)
  │
  ├─ 5. 记录结果到 ledger
  │     ├─ COMPLETED (可重放)
  │     ├─ FAILED (不可重放)
  │     └─ TIMEOUT (不可重放)
  │
  └─ 6. 返回 ToolResult
```

### ToolExecutionLedger

```
ledger.entries: dict[key, ToolLedgerEntry]

只有 COMPLETED 状态可以重放:
  - FAILED → 需要重新执行
  - TIMEOUT → 需要重新执行
  - IN_PROGRESS → 等待完成
```

### 幂等 Key 设计

```python
idempotency_key = f"invoke:{tool_name}:{sha256[:32]}"

# sha256 输入: conversation_id + tool_name + normalized_args
# 不包含 run_id/tool_call_id (重试时会变)
```

---

## 6. ToolSelector (Agent 选择 Tool)

```
AgentLoop → ToolSelector.select(goal, tool_catalog, requested_tool?):
  │
  ├─ requested_tool 存在:
  │   └─ 验证在 catalog 中存在 → 返回
  │       不存在 → ToolSelectionError
  │   (从不做 capability → tool 的位置映射)
  │
  └─ requested_tool 不存在:
      └─ LLM 从 ToolMetadata catalog 中选择
         输入: goal.required_capabilities + tool descriptions
         输出: SelectedTool {name, reasoning}
```

**禁止 `capability.tools[0]`**: 永远不按位置选第一个 tool。LLM 根据 tool 的 description metadata 选择最合适的。

---

## 7. 完整调用链

```
Agent Decision
  │
  ├─ ToolSelector.select(goal, catalog)
  │   └─ LLM 选择: "creator.create_draft"
  │
  ├─ ToolPolicyGate.enforce("creator.create_draft")
  │   └─ policy: IDEMPOTENT_WRITE, cost=HIGH → QUEUE
  │
  ├─ GoalCompiler.compile_plan → TaskPlan (单步)
  │   └─ ExecutionInput.from_executable_plan
  │
  ├─ ExecutionSubmissionService.submit(plan)
  │   ├─ ExecutionStateManager.init_execution
  │   └─ execution_queue.enqueue → READY
  │
  ├─ (Queue Worker 消费)
  │
  ├─ ExecutionWorker.run()
  │   └─ CapabilityExecutor.execute_step()
  │       ├─ tool_name = "content.create_draft"
  │       ├─ 参数绑定: ArgumentBinder.bind(tool_name, args, plan_context)
  │       │   └─ 注入 session.active_draft_id, trace_id 等
  │       │   └─ 校验参数对 Pydantic schema
  │       └─ ToolRuntime.invoke(ctx)
  │           ├─ ledger replay? → 否
  │           ├─ raw_handler(tool_name, args)
  │           │   └─ GreenBookMCPServer.execute_tool()
  │           │       ├─ schema 校验 (Pydantic)
  │           │       ├─ handler(ctx, **args)
  │           │       │   └─ content.create_draft(ctx, title, instruction, ...)
  │           │       │       ├─ CreatorClient.create_task(...)
  │           │       │       ├─ CreatorClient.wait_for_completion(...)
  │           │       │       └─ JavaClient.create_draft(...)
  │           │       └─ 输出校验 (ToolResult)
  │           └─ ledger.record_complete(key, result)
  │
  └─ ToolResult → Observation → AgentLoop.Reflect
```

---

## 8. 为什么 Tool Runtime 独立存在？

1. **统一执行边界**: 所有 Tool 调用经过同一个入口，schema 校验、幂等检查、超时、错误分类在一个地方完成。

2. **安全 handshake**: Tool Runtime 编排 Creator (内容生成) + Java (社区持久化) 的多步流程，包括 write-then-verify、乐观锁 version 匹配。

3. **上下文注入**: ToolContext (identity, session, trace IDs) 由 Runtime 注入 handler，不从 LLM 参数中获取——防止 prompt injection。

4. **失败分类**: 所有下游错误统一映射到 ToolResult 信封，Worker/ledger/recovery 基于一致的 failure semantics 做决策。

5. **策略解耦**: 策略定义 (`TOOL_POLICY_CATALOG`) 和执行 (MCP handlers) 分开。策略在一个地方定义，多个地方消费 (security, planning, agent loop)。
