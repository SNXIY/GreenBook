# GreenBook 模块通信方式

## 1. 通信总览

```
Frontend (Vite)
  │ HTTP/SSE (Bearer JWT)
  ▼
Agent API (FastAPI)
  │ Queue (PostgreSQL)          │ HTTP (Bearer JWT)
  ▼                             ▼
Agent Worker (Async)         Creator Service (FastAPI)
  │ Tool Contract               │
  ▼                             │
MCP Runtime (in-process)        │
  │ HTTP (Bearer JWT)           │
  ▼                             │
Java Backend (Spring) ◄─────────┘ (Handoff)
```

---

## 2. Frontend → Agent API

**协议**: HTTP/1.1 + SSE

**端点**:
- `POST /api/v1/agent/conversations/{id}/messages` — 发送消息
- `GET /api/v1/agent/conversations/{id}/messages` — 获取历史
- `GET /api/v1/executions/{id}/stream` — SSE 事件流
- `POST /api/v1/executions/{id}/approve` — 审批决定

**认证**: Bearer RS256 JWT (Java 签发)

**流式**: 202 Accepted 返回 `events_url`，前端通过 SSE 重放事件存储

**消息格式**:
```json
// Request
{
  "content": "帮我写一篇Java文章，明天发布",
  "timezone": "Asia/Shanghai"
}

// Response (202)
{
  "run_id": "abc123",
  "execution_id": "exec_xyz",
  "status": "QUEUED",
  "events_url": "/api/v1/executions/exec_xyz/events"
}
```

---

## 3. Agent API → Worker

**方式**: PostgreSQL 执行队列

**队列消息**:
```
ExecutionQueueMessage:
  execution_id: "exec_xyz"     # 唯一, 一个 execution 一条消息
  status: READY|CLAIMED|ACKED|FAILED
  claimed_by: "agent-retry-worker"
  claim_until: "2026-08-12T10:05:00Z"
  attempt: 1
  payload: {                   # 无 secrets!
    execution_input: {...}     # 序列化的 ExecutionInput
    task_context: {...}
    session_context: {...}
    auth_context: {            # tokens 已剥离
      user_id: "...",
      tenant_id: "..."
    }
  }
```

**流程**:
```
Agent API:
  submit_plan → init_execution(PENDING) → enqueue(READY)

Worker:
  claim → READY→CLAIMED
  handler → ExecutionWorker.run()
  success → ack → CLAIMED→ACKED
  failure → fail → CLAIMED→FAILED
  deferred → release → CLAIMED→READY
```

**凭证传递**: Queue 消息的 payload 中 tokens 被剥离。进程内 Worker 通过 `ExecutionCredentialBroker` 恢复凭证（进程内内存存储）。独立 Worker 通过 `GREENBOOK_AGENT_WORKER_ACCESS_TOKEN` 环境变量获取服务凭证。

---

## 4. Worker → Agent Core

**方式**: 进程内函数调用 (library)

Worker 本身是薄组装层，所有执行逻辑来自 `packages/agent_core/execution/`:

```python
# Worker 进程
execution_queue_worker = ExecutionQueueWorker(
    queue=persistence.execution_queue,
    handler=RuntimeExecutionQueueHandler(
        service=runtime_agent_service,
        completion_publisher=completion_publisher,
        ...
    ),
    lease_manager=persistence.lease_manager,
    ...
)

# handler(message) 内部调用
runtime_agent_service.execute_queued(message, mcp, llm, model, auth)
  → _execute_single()
    → CapabilityMapper → PlanValidator → ExecutionWorker.run()
      → CapabilityExecutor → ToolRuntime → MCP
```

---

## 5. Agent → Tool Runtime

**方式**: Tool Contract (内存函数调用)

```
AgentLoop.ToolSelector.select(goal, tool_catalog, llm)
  ↓ (LLM 返回 tool_name)
AgentLoop.ToolPolicyGate.enforce(tool_name)
  ↓ (DENY | QUEUE | SYNC)
ToolRuntime.invoke(invocation_context)
  ↓ (幂等检查 → 执行 → 记录)
GreenBookMCPServer.execute_tool(tool_name, **args)
  ↓ (schema 校验 → handler 调用 → 输出校验)
handler(ctx, **args) → ToolResult
```

**Tool 选择**: LLM 根据 ToolMetadata (name, description, capabilities) 选择，不是按 capability.tools[0] 固定映射。

**Policy 检查**: ToolPolicyGate 读取 ToolPolicyMetadata 决定执行模式。

**幂等保护**: Ledger 记录每次 tool invocation，COMPLETED 状态可直接重放。

---

## 6. Tool Runtime → MCP

**方式**: MCP-compatible in-process call

```
ToolRuntime.invoke(ctx)
  → raw_handler(tool_name, tool_args)
    → GreenBookMCPServer.execute_tool(
        tool_name,
        auth=ctx.auth,
        session=session,
        trace_id=...,
        agent_run_id=...,
        tool_call_id=...,
        **tool_args
      )
      → tool_registry.get_tool(name)
      → input_schema.model_validate(args)
      → handler(ctx, **args) → ToolResult
      → output_schema.model_validate(result)
```

MCP Server 是**进程内对象**，不通过 HTTP/stdio。调用是直接函数调用。

**为什么不在独立进程？**

- 低延迟（无网络跳数）
- 上下文传递（`AuthContext` 含 JWT token，不能序列化进 Queue）
- 简化部署（无需额外的 MCP 进程、服务发现、健康检查）

---

## 7. MCP → Java Backend

**协议**: HTTP/1.1 (httpx AsyncClient)

**认证**: Bearer RS256 JWT (AuthContext.raw_access_token 中继)

**Headers**:
```
Authorization: Bearer <user_jwt>
Idempotency-Key: greenbook:create_draft:<sha256>
X-Trace-ID: <trace_id>
X-Conversation-Id: <conv_id>
X-Agent-Run-Id: <run_id>
X-Tool-Call-Id: <tool_call_id>
```

**路径**: `/api/v1/agent/**` (Agent Facade)

**端点**: 搜索帖子 / 草稿 CRUD / 定时发布 / 评论 / 分析 (共 16 个端点)

---

## 8. MCP → Creator Service

**协议**: HTTP/1.1 (httpx AsyncClient)

**路径**: `/api/v1/creator/**`

**流程**:
```
content.create_draft handler:
  1. CreatorClient.create_task(kind=CREATE_CONTENT, ...)
     → POST /api/v1/creator/tasks (Idempotency-Key)
  2. CreatorClient.wait_for_completion(task_id, deadline=240s)
     → GET /api/v1/creator/tasks/{id} (轮询)
  3. CreatorClient.get_artifact(task_id, artifact_id)
     → GET /api/v1/creator/tasks/{id}/artifacts/{id}
```

---

## 9. Assistant → Creator

**方式**: Capability 调用链

Main Agent 不直接调用 Creator。调用通过 MCP content handler：

```
Main Agent → AgentLoop → ToolSelector → "content.create_draft"
  → ToolRuntime → MCP → content.create_draft handler
    → CreatorClient.create_task(...)     # HTTP to Creator
    → CreatorClient.wait_for_completion  # Poll
    → CreatorClient.get_artifact         # HTTP
    → JavaClient.create_draft(...)       # HTTP to Java
```

**为什么 Assistant 不直接包含 Creator 逻辑？**

1. Creator 是长时间运行的多步任务（Research→Strategy→Write→Critique），需要独立的持久化控制面
2. Creator 有自己的 Human-in-the-Loop 审批点（主题选择、大纲确认、草稿审查）
3. Creator 需要独立的扩缩容和资源（GPU/内存 for models, Qdrant for RAG）
4. Creator 有自己的评估框架和 offline regression
5. 按业务边界拆分：Assistant 管理对话和任务编排，Creator 管理内容创作管线

---

## 10. 通信模式总结

| 方向 | 协议 | 边界 | 认证 |
|------|------|------|------|
| Frontend → API | HTTP/SSE | 网络 | Bearer JWT (RS256) |
| API → Worker | PostgreSQL Queue | 进程 | Queue claim + Worker token |
| Worker → Agent Core | In-process | 内存 | — (library) |
| Agent → Tool Runtime | Tool Contract | 内存 | — (function) |
| Tool Runtime → MCP | In-process | 内存 | — (function) |
| MCP → Java | HTTP | 网络 | Bearer JWT (中继) |
| MCP → Creator | HTTP | 网络 | Bearer JWT (中继) |
| Creator → Java | HTTP | 网络 | HMAC Handoff Secret |
| API → Worker | Queue + Lease | DB | Worker ID + lease TTL |
