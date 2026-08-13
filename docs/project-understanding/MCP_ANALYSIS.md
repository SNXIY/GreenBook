# GreenBook MCP Runtime Analysis

## 1. 定位

GreenBook MCP (`services/greenbook_mcp`) 是 **MCP-compatible in-process tool runtime**，不是独立部署的 MCP 服务器。

---

## 2. 项目结构

```
services/greenbook_mcp/
├── pyproject.toml
└── greenbook_mcp_server/
    ├── server.py              # GreenBookMCPServer (入口)
    ├── context.py             # ToolContext (注入到每个 handler)
    ├── tool_registry.py       # 16 个 handler 注册
    ├── tool_schemas.py        # 15 个 Pydantic 参数模型
    └── tools/
        ├── analytics.py       # get_post_performance, get_account_summary
        ├── community.py       # search_public_posts, get_post, list_own_posts
        ├── content.py         # create_draft, get_draft, list_drafts, revise_draft
        ├── interaction.py     # list_comments, send_reply
        └── publication.py     # schedule, get_status, update, cancel, publish_now
```

---

## 3. 为什么使用 MCP 概念？

虽然 `mcp>=1.10` 声称为依赖但**从未导入**（没有 FastMCP、没有 stdio/SSE transport、没有 JSON-RPC），MCP 层存在的原因：

### 1. 统一执行边界

`GreenBookMCPServer.execute_tool()` 集中了：
- Schema 校验 (Pydantic input/output)
- Handler 签名检查
- 统一 `ToolResult` 信封
- 失败分类 (区分 "请求未发送" vs "结果未知")

如果直接 HTTP 调用，这些逻辑会分散在每个调用点。

### 2. 安全的写操作握手

`content.create_draft` 编排 Creator + Java 的多步流程：
```
Creator 生成内容 → 等待完成 → 获取产物 → Java 保存草稿 → GET 验证
```
这个编排逻辑在一处定义，不在 Runtime 中分散。

### 3. 上下文注入

每个 handler 的第一个参数是 `ToolContext`：
```python
ToolContext:
  auth: AuthContext           # 用户身份 (JWT)
  session: SessionContext     # 会话状态 (active_draft_id, ...)
  java: JavaClient            # Java Backend 客户端
  creator: CreatorClient      # Creator Service 客户端
  trace_id: str               # 追踪 ID
  tool_call_id: str           # Tool 调用 ID
```

身份由 runtime 注入，**不从 LLM 参数中获取**——防止 prompt injection。

### 4. 安全/策略解耦

MCP 持有执行；策略目录 (`TOOL_POLICY_CATALOG`) 在 `packages/contracts` 中定义。MCP 在构造时校验自己的 contracts 与策略目录一致，阻止第二份漂移的策略目录。

### 5. 失败分类边界

所有下游失败统一映射到 `ToolResult` 信封（包含 `retryable`, `request_sent`, `state.phase`, `safe_to_retry`），Worker/ledger/recovery 基于一致的语义决策。

### 6. Session 状态作为跨步骤记忆

Handler 修改 `SessionContext` (`active_draft_id`, `active_schedule_id`)，后续 PlanStep 可以解析资源。这个"会话工作集"由 runtime 掌控，不由 HTTP 客户端管理。

---

## 4. MCP 和普通 HTTP Client 的区别

| 方面 | 普通 HTTP Client | MCP-compatible Runtime |
|------|------------------|------------------------|
| 调用方式 | `java.search_posts(query)` | `mcp.execute_tool("community.search_public_posts", query=...)` |
| 参数校验 | 无或分散 | Pydantic schema 统一校验 |
| 结果封装 | 返回业务对象 | 统一 ToolResult 信封 |
| 上下文注入 | 每次手动传递 | ToolContext 自动注入 |
| 多步编排 | 每处复制 | 在 handler 中统一实现 |
| 错误分类 | 原始 HTTP 错误 | 统一的 `retryable/request_sent/side_effect` 语义 |
| LLM 可见 | 无 | `get_tool_definitions()` 导出 function-calling schema |

---

## 5. 为什么是 Process-in Runtime 而不是独立服务？

1. **低延迟**: Agent 调用 Tool 没有网络跳数。同一个 Python 进程中直接函数调用。

2. **简化部署**: 不需要额外的 MCP 进程、服务发现、健康检查。API 和 Worker 各自构造一个 `GreenBookMCPServer` 实例。

3. **上下文传递**: `ToolContext` 包含了 JWT token（不能序列化进消息队列）和 `SessionContext`（进程内可变状态）。独立服务需要序列化这些敏感数据。

4. **已有 HTTP 边界**: Java Backend 和 Creator Service 已经提供了网络边界。MCP 层的价值在于统一调用协议，不需要再加一层网络跳转。

5. **MCP 协议过度**: 真正的 MCP 协议（JSON-RPC + capability negotiation + transport）对于内部调用是一种过度设计。当前的方式保留了 MCP 的概念优势（工具描述、统一信封），而不需要协议开销。

---

## 6. 注册的 16 个 Tools

| Tool | 类别 | 风险 | 审批 |
|------|------|------|------|
| community.search_public_posts | community | READ | 否 |
| community.get_post | community | READ | 否 |
| community.list_own_posts | community | READ | 否 |
| content.create_draft | content | IDEMPOTENT_WRITE | 否 |
| content.get_draft | content | READ | 否 |
| content.list_drafts | content | READ | 否 |
| content.revise_draft | content | IDEMPOTENT_WRITE | 否 |
| publication.schedule | publication | IDEMPOTENT_WRITE | 否 |
| publication.get_status | publication | READ | 否 |
| publication.update_schedule | publication | IDEMPOTENT_WRITE | 否 |
| publication.cancel_schedule | publication | IDEMPOTENT_WRITE | 否 |
| publication.publish_now | publication | **DESTRUCTIVE_WRITE** | **是** |
| interaction.list_comments | interaction | READ | 否 |
| interaction.send_reply | interaction | **DESTRUCTIVE_WRITE** | **是** |
| analytics.get_post_performance | analytics | READ | 否 |
| analytics.get_account_summary | analytics | READ | 否 |

---

## 7. Handler 签名约定

所有 handler 遵循统一签名：

```python
async def handler(
    ctx: ToolContext,          # 第一个参数总是 ctx
    **kwargs: PydanticModel    # 剩余参数匹配 input_schema 字段
) -> ToolResult:               # 总是返回 ToolResult
    ...
```

`validate_registered_tool_contracts()` 在构造时强制检查：
- handler 参数名 == Pydantic model 字段名
- handler 返回值是 `ToolResult`

---

## 8. 完整的 Handler 执行流程

```
GreenBookMCPServer.execute_tool(name, auth, session, ..., **kwargs)
  │
  ├─ 1. tool_registry.get_tool(name)
  │     └─ 未知 → VALIDATION_ERROR 信封
  │
  ├─ 2. definition.input_schema.model_validate(kwargs)
  │     └─ 失败 → INVALID_TOOL_ARGUMENT / TOOL_ARGUMENT_VALIDATION_FAILED
  │
  ├─ 3. 校验 handler 签名
  │     └─ 失败 → PRE_EXECUTION_VALIDATION_FAILED
  │
  ├─ 4. 构建 ToolContext
  │     ctx = ToolContext(auth, session, java, creator, trace_id, ...)
  │
  ├─ 5. result = await handler(ctx, **normalized_kwargs)
  │
  ├─ 6. output_schema.model_validate(result)
  │     └─ 失败 → TOOL_OUTPUT_VALIDATION_FAILED
  │
  └─ 7. 返回 validated_result.model_dump(mode="json")
```

---

## 9. Content 工具的创建流程

`content.create_draft` 的完整 6 步流程：

```
1. 构建引用笔记 (上限 8 条 / 12000 字符)
2. Client.create_task(kind=CREATE_CONTENT, interaction_mode=AUTO)
   → Creator 生成内容
3. Client.wait_for_completion(deadline=240s)
   → 轮询直到完成
4. Client.get_artifact(task_id, artifact_id)
   → 提取 title/body_markdown
5. Java.create_draft(title, body, idempotency_key)
   → Java 持久化草稿 (幂等)
6. Java.get_draft(draft_id)
   → 验证创建成功 → 更新 session.active_draft_id
```
