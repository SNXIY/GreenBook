# GreenBook Agent Runtime — 现状分析与渐进式重构方案

> 日期: 2026-08-07
> 状态: 分析阶段 — 不修改代码
> 前置阅读: `docs/reports/greenbook-agent-runtime-architecture-v3.md`（架构设计）
>
> 本文档聚焦于 **当前代码的真实状态** + **差距分析** + **具体迁移步骤**。

---

# 第一部分：当前代码理解

## 1. 项目目录结构

```
D:\agent\green-book\
├── apps/
│   ├── assistant_api/          # FastAPI :8094 — 当前运行的主服务
│   │   └── greenbook_assistant_api/
│   │       ├── main.py         # App 工厂、JWT 中间件、lifespan、/health
│   │       ├── api/
│   │       │   └── routes.py   # 所有路由（~1240行）— 核心调度逻辑
│   │       ├── dependencies/   # 空壳（Auth 通过 request.state 注入）
│   │       └── streaming/      # 空壳
│   └── assistant_worker/       # 空壳（Kafka/scheduler Worker 未实现）
│
├── packages/
│   ├── assistant_core/         # Agent 核心循环
│   │   └── greenbook_assistant_core/
│   │       ├── agent.py        # CommunityOperationsAssistant — 主循环
│   │       ├── context.py      # SessionContext、RecentEntity、PendingApproval
│   │       ├── memory.py       # ConversationMemory 空壳
│   │       ├── middleware.py   # TraceMiddleware（未被使用）
│   │       ├── time_parser.py  # 中文相对时间确定性解析
│   │       ├── prompts/        # DEFAULT_SYSTEM_PROMPT（未被使用）
│   │       └── skills/         # 空壳
│   ├── contracts/              # 共享类型：ToolResult、AuthContext、ErrorCode
│   ├── java_client/            # Java REST API 异步客户端
│   ├── creator_client/         # Creator Agent 异步客户端
│   ├── security/               # JWT 验证、AuthResolver、审批策略
│   ├── observability/          # OpenTelemetry 追踪
│   └── evaluation/             # E2E 和契约测试
│
├── services/
│   ├── greenbook_mcp/          # MCP 工具服务（当前为进程内调用）
│   │   └── greenbook_mcp_server/
│   │       ├── server.py       # GreenBookMCPServer — 工具分发
│   │       ├── tool_registry.py # 16 个工具注册
│   │       ├── tool_schemas.py  # Pydantic 参数模型
│   │       ├── context.py      # ToolContext（auth+session+java+creator 注入）
│   │       ├── tools/          # 工具 handler
│   │       │   ├── community.py   # search_public_posts, get_post, list_own_posts
│   │       │   ├── content.py     # create_draft, get_draft, list_drafts, revise_draft
│   │       │   ├── publication.py # schedule, update, cancel, publish_now
│   │       │   ├── interaction.py # list_comments, send_reply
│   │       │   └── analytics.py   # get_post_performance, get_account_summary
│   │       └── workflows/     # 旧版工作流（已被 tools/ 取代）
│   └── creator_agent/         # Creator Agent — LangGraph 内容创作服务 :8092
│
├── community-assistant-agent/  # 旧版（待 E2E 验证后废弃）
├── greenbook-backend/          # Java Spring Boot 后端
├── greenbook-frontend/         # React 前端
└── docs/
    └── reports/
        └── greenbook-agent-runtime-architecture-v3.md  # 架构设计（参考但不照搬）
```

## 2. 当前真实调用链

以下是一次用户请求的完整调用链（以 "帮我写一篇Java文章，明天8点发布" 为例）：

```
Step 1 — HTTP 入口
──────────────────
POST /api/v1/assistant/conversations/{id}/messages
Authorization: Bearer <greenbook JWT>
Idempotency-Key: <uuid>

    ↓

Step 2 — JWT 认证
─────────────────
_JwtAuthMiddleware.dispatch()                           # main.py:41-97
    → validate_access_token(token, jwks_url, ...)       # packages/security/jwt.py
    → AuthContext(user_id, tenant_id, roles, ...)        # packages/contracts/identity.py
    → request.state.auth_context = auth_context

    ↓

Step 3 — 会话加载
─────────────────
routes.py:send_message()                                # routes.py:629
    → _get_auth(request) → AuthContext
    → _get_session(request, conversation_id)
        → conversation_store[conversation_id]             # 内存 dict
        → _conversation_belongs_to(auth, data)            # 所有权校验
        → SessionContext(**data)                           # context.py

    ↓

Step 4 — 历史回放
─────────────────
    → message_store[conversation_id]                      # 内存 dict
    → 仅 replay user/assistant 消息（不含 tool observation）

    ↓

Step 5 — 工具 Schema 构建
─────────────────────────
    → _build_tool_schemas()                              # routes.py:360-556
    → 16 个 OpenAI function-calling JSON schema
    → 注意：LLM 看到的工具名用下划线 (content_create_draft)
    →      MCP 注册的工具名用点号 (content.create_draft)

    ↓

Step 6 — Agent 构造与执行
─────────────────────────
    → CommunityOperationsAssistant(llm, model, tools_schema, system_prompt)
    → assistant.run(user_message, session, tool_handler, ...)

        内部执行（agent.py:273-543）:

        Step 6a — 确定性意图检测
        ────────────────────────
        _turn_intents(user_message)                       # agent.py:99-114
            → (asks_create=True, asks_revise=False,
               asks_schedule=True, asks_cancel=False, asks_search=False)
            → 匹配规则：关键词 "写一篇" + "明天8点"

        _turn_routing_hint(user_message, session)          # agent.py:132-189
            → "INTERNAL TURN ROUTING: This is a create-and-schedule request.
               Call content_create_draft first, then publication_schedule..."

        _turn_tool_filter(user_message, session)           # agent.py:193-224
            → {"content_create_draft"}  # 第一轮只暴露创建工具

        Step 6b — 系统提示词组装
        ────────────────────────
        _build_system_prompt(session)                     # agent.py:253-271
            → 基础 prompt + 会话上下文 (conversation_id, 时间, 时区,
               active_draft_id, active_schedule_id)

        Step 6c — LLM 调用循环
        ──────────────────────
        messages = [system_prompt, routing_hint, history..., user_message]
        tools = [content_create_draft]  # 被 filter 限制

        while tool_rounds < 30:
            resp = llm.chat.completions.create(
                model="deepseek-v4-flash",
                messages=messages,
                tools=turn_tools,
                tool_choice="auto",
                temperature=0.0,
            )

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_name = "content_create_draft"
                    tool_args = json.loads(tc.function.arguments)

                    # 回调 routes.py 中的 tool_handler
                    result = await tool_handler(
                        tool_name, tool_args, session, run_id, tc.id)

                    # tool_handler 内部:
                    #   1. 名称转换: content_create_draft → content.create_draft
                    #   2. _bind_target_tool_args: 注入 session.active_draft_id
                    #   3. 注入 community_references（如果之前搜索过）
                    #   4. 审批检查 requires_approval()
                    #   5. await mcp.execute_tool(mcp_name, ...)

                    # 观察结果追加到 messages
                    observation = {"tool_name": ..., "ok": True/False, ...}
                    messages.append({"role": "tool", "content": observation})

                    # 成功后更新 session + 展开下一轮工具
                    if tool_name == "content_create_draft" and ok:
                        session.active_draft_id = data["draft_id"]
                        turn_tools = [publication_schedule]  # 展开 schedule

            else:
                final_content = msg.content
                break

        Step 6d — MCP 工具执行
        ──────────────────────
        GreenBookMCPServer.execute_tool("content.create_draft", ...)
            → tool_registry.get_tool("content.create_draft")
            → Pydantic 校验（如有 argument_model）
            → 签名绑定检查
            → ToolContext(auth, session, java, creator, ...)
            → handler(ctx, **kwargs)

                content.create_draft() 内部:
                    1. ctx.creator.create_task(kind="CREATE_CONTENT", goal=instruction)
                    2. ctx.creator.wait_for_completion(task_id, deadline=240s)
                    3. ctx.creator.get_artifact(task_id, final_artifact_id)
                    4. extract_creator_document() → title/body/summary
                    5. ctx.java.create_draft(AgentDraftCreateRequest, idempotency_key)
                    6. ctx.java.get_draft(draft_id)  — GET 验证
                    7. ctx.session.active_draft_id = draft_id
                    8. 返回 ToolResult.success({draft_id, title, ...})

            → 返回 ToolResult 封包到 routes.py

    ↓

Step 7 — 结果处理
─────────────────
    → 工具结果存入 events[]（用于 SSE）
    → assistant_content = _append_schedule_confirmation(...)
    → message_store[conv_id].append({role: "assistant", content})
    → _save_session(request, session)
    → run_store[run_id] = {...}
    → 返回 202 RunAcceptedResponse

    ↓

Step 8 — 前端轮询
─────────────────
    → GET /api/v1/assistant/runs/{run_id}/events
    → SSE 回放 events[]
```

### 关键观察

从调用链可以看出当前系统真实的工作方式：

1. **意图检测在 agent.run() 内部**，通过纯中文关键词正则完成，**不是独立的模块**
2. **多步骤任务的顺序控制**通过 `_turn_tool_filter()` + 成功后动态展开 `turn_tools` 实现
3. **"搜索→创建→发布"的编排**完全硬编码在 agent.py 的 if-else 逻辑中（行 490-522）
4. **没有持久化** — Conversation、Message、Run 全部在内存 dict 中
5. **MCP 是进程内调用**，不是远程 HTTP 协议
6. **ToolContext 由 routes.py 构造并注入**，包含 auth、session、java、creator 四个依赖

## 3. 当前各模块职责

```
┌──────────────────────────────────────────────────────────────────┐
│                    routes.py (apps/assistant_api)                 │
│  职责：HTTP 层 + 会话管理 + 工具执行调度                           │
│                                                                   │
│  关键代码：                                                       │
│  - send_message() — 核心入口，组装所有组件并驱动执行               │
│  - tool_handler 回调 — 名称转换、参数绑定、审批门控、MCP 调用      │
│  - _build_tool_schemas() — 硬编码 16 个工具的 OpenAI JSON Schema  │
│  - 状态管理: conversation_store, run_store,                       │
│              approval_store, message_store (全部内存 dict)        │
│  - SSE 事件发射和回放                                             │
│  - 错误映射 _http_status_for_tool_error()                         │
│                                                                   │
│  问题：职责过重。~1240 行同时包含：                                │
│    路由定义 + 会话管理 + 工具调度 + 审批 + SSE + 错误处理          │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                  agent.py (packages/assistant_core)               │
│  职责：Agent 循环 + 意图检测 + 工具过滤                            │
│                                                                   │
│  关键代码：                                                       │
│  - CommunityOperationsAssistant.run() — LLM 调用循环（max 30轮）  │
│  - _turn_intents() — 中文关键词意图检测（5 元组布尔）             │
│  - _turn_routing_hint() — 路由提示注入到 system prompt            │
│  - _turn_tool_filter() — 工具白名单过滤                           │
│  - 顺序工具门控 — search→create→schedule 等硬编码逻辑             │
│  - PRODUCT_DEFAULTS — 产品默认语义常量                            │
│                                                                   │
│  问题：                                                           │
│  - 意图检测是正则，不理解语义相似性                                │
│  - 多步骤编排硬编码（6 种组合的 if-else）                         │
│  - 无 Task 概念，Run 是瞬时的                                     │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                 context.py (packages/assistant_core)              │
│  职责：会话状态模型                                               │
│                                                                   │
│  SessionContext:                                                  │
│  - conversation_id, user_id(frozen), tenant_id(frozen)            │
│  - active_draft_id, active_post_id, active_schedule_id            │
│  - recent_entities: list[RecentEntity]  (最多 20)                 │
│  - recent_tool_calls: list[RecentToolCall]  (最多 20)             │
│  - pending_approval: PendingApproval | None                       │
│  - resolve_active_draft_id(), resolve_active_schedule_id()        │
│                                                                   │
│  问题：                                                           │
│  - active_*_id 是扁平字段，无法管理多个同类型实体                  │
│  - recent_entities 是简单列表，非结构化 Task 索引                  │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│              GreenBookMCPServer (services/greenbook_mcp)          │
│  职责：工具分发 + Pydantic 校验 + ToolContext 注入                │
│                                                                   │
│  execute_tool():                                                  │
│  1. tool_registry 查找                                            │
│  2. Pydantic argument_model 校验                                  │
│  3. 签名绑定检查                                                  │
│  4. ToolContext 构造 → handler(ctx, **kwargs)                     │
│                                                                   │
│  工具 handlers (tools/*.py):                                      │
│  - 编排 Creator + Java 调用                                       │
│  - GET-after-write 验证                                          │
│  - 状态机守卫（schedule 的 SCHEDULED→CANCELLED 等）               │
│  - 乐观锁版本控制（expectedVersion）                              │
│                                                                   │
│  优点：职责清晰，确定性边界好，工具实现质量高                        │
└──────────────────────────────────────────────────────────────────┘
```

## 4. 哪些代码可以保留（零改动）

| 模块 | 文件 | 理由 |
|------|------|------|
| MCP 工具层 | `services/greenbook_mcp/tools/*.py` | 实现质量高，GET-verify、状态机守卫、乐观锁、idempotency 都正确 |
| MCP 工具注册 | `services/greenbook_mcp/tool_registry.py` | 注册机制清晰，contract validation 可靠 |
| MCP 分发 | `services/greenbook_mcp/server.py` | Pydantic 校验 + ToolContext 注入模式正确 |
| ToolContext | `services/greenbook_mcp/context.py` | 依赖注入设计好，idempotency_key 生成稳定 |
| 时间解析 | `packages/assistant_core/time_parser.py` | 中文相对时间确定性解析，不应由 LLM 做 |
| Java Client | `packages/java_client/` | 完整的 REST 客户端，错误映射完善 |
| Creator Client | `packages/creator_client/` | 完整的 Creator API 客户端 |
| 安全层 | `packages/security/` | JWT 验证、AuthResolver、审批策略 |
| 契约类型 | `packages/contracts/` | ToolResult、AuthContext、ErrorCode |
| JWT 中间件 | `apps/assistant_api/main.py:_JwtAuthMiddleware` | 认证逻辑正确 |

## 5. 哪些代码需要逐步替换

| 当前代码 | 位置 | 替换原因 | 替换目标 |
|---------|------|---------|---------|
| `_turn_intents()` | `agent.py:99-114` | 中文关键词正则，不理解语义 | TaskUnderstanding.understand() |
| `_turn_routing_hint()` | `agent.py:132-189` | 硬编码的 if-else 路由规则 | TaskIntent.relation + Planner |
| `_turn_tool_filter()` | `agent.py:193-224` | 硬编码的工具白名单 | Planner → CapabilityDAG → Execution Engine |
| 顺序工具门控 | `agent.py:490-522` | 6 种组合硬编码 | Execution Engine 的 DAG 拓扑执行 |
| `_build_tool_schemas()` | `routes.py:360-556` | 硬编码 16 个 JSON Schema | 动态从 tool_registry 生成 |
| `SessionContext.active_*_id` | `context.py` | 扁平字段无法管理多实体 | Task.artifacts[] + TaskRegistry |
| 内存 dict stores | `routes.py` + `main.py` | 进程重启丢失 | PostgreSQL 持久化 |
| `ConversationMemory` | `memory.py` | 空壳，不工作 | 真正的 DB-backed memory |

---

# 第二部分：架构差距分析

## Gap 1：意图理解 — 从关键词到语义

### 当前状态

`agent.py` 中的 `_turn_intents()`:

```python
_CREATE_MARKERS = ("草稿", "保存", "存为", "写一篇", "写个", "创作一篇", ...)
_SCHEDULE_MARKERS = ("定时", "安排", "后发布", "发布任务")
# ...

def _turn_intents(user_message: str) -> tuple[bool, bool, bool, bool, bool]:
    text = user_message.strip().lower()
    asks_create = any(word in text for word in _CREATE_MARKERS)
    asks_revise = any(word in text for word in ("修改", "改成", "改得", "润色", "重写"))
    asks_schedule = any(word in text for word in _SCHEDULE_MARKERS) or ...
    asks_cancel = any(word in text for word in ("取消", "撤销"))
    asks_search = any(word in text for word in ("搜索", "查找", "检索"))
    return asks_create, asks_revise, asks_schedule, asks_cancel, asks_search
```

### 问题

```
"参考优秀文章优化一下"      → 关键词 "优化" 不在任何列表中
"借鉴热门内容重新整理"      → 关键词 "整理" 不在任何列表中
"提升文章质量"              → 关键词 "提升" 不在任何列表中
"让这篇文章更好"            → 无任何关键词匹配
```

这些语义上都应识别为 `IMPROVE_CONTENT`（修改已有草稿），但关键词匹配将它们分散到不同的处理分支或回退到默认行为。

### 目标

```python
# 输入: 自然语言
"参考优秀文章优化一下"

# 输出: TaskIntent（LLM 生成，Pydantic 校验）
TaskIntent(
    relation="MODIFY_TASK",           # 修改已有任务
    goal_category="IMPROVE_CONTENT",  # 统一语义类别
    goal="改进当前草稿的内容质量，参考社区优秀文章的风格",
    target_task_hint="当前草稿",
    requirements=[
        Requirement(type="SEARCH", params={"topic": "优秀文章"}),
        Requirement(type="ANALYZE", params={"aspect": "writing_style"}),
        Requirement(type="IMPROVE", params={"target": "current_draft"}),
    ],
    constraints=[
        Constraint(type="REFERENCE", value="community_top_posts"),
    ],
)
```

### 实现方式

**不是**增加更多关键词。**不是**恢复旧版的复杂 `IntentDeltaParser._operation()`。

**而是**用一个轻量的 LLM 调用，将自然语言转换为结构化 JSON：

```
User Message + Session Context (已有 Task 列表)
    ↓
LLM Prompt（约 300 tokens，精心设计的 few-shot）
    ↓
TaskIntent（Pydantic model_validate 校验）
    ↓
校验失败 → 回退到旧版关键词匹配（安全网）
```

保留 `_turn_intents()` 作为快速路径：对于 "列出我的草稿"、"取消定时任务" 等明显单步操作，不走 LLM。

---

## Gap 2：任务拆解 — 从 Tool Call 到 Capability DAG

### 当前状态

当前系统**没有 Planner**。多步骤任务的"规划"由两块拼凑而成：

1. **LLM 隐式推理** — 模型在自己的 reasoning 中决定先 search 再 create
2. **代码硬编码的顺序门控** — agent.py 行 490-522:

```python
if tool_name == "community_search_public_posts":
    if search_then_create:
        turn_tools = [content_create_draft]   # 搜索完→展开创建
    else:
        turn_tools = []

elif tool_name == "content_create_draft":
    if create_then_schedule and session.active_draft_id:
        turn_tools = [publication_schedule]    # 创建完→展开发布
    else:
        turn_tools = []

elif tool_name == "content_revise_draft":
    if revise_then_schedule:
        schedule_tool = _schedule_tool_for_session(session)
        turn_tools = [schedule_tool]           # 修改完→展开定时调整
    else:
        turn_tools = []
```

支持的组合：
- `search → create`（搜索后创建）
- `create → schedule`（创建后发布）
- `revise → schedule`（修改后调整发布时间）
- `search → create → schedule`（完整三步骤）
- `cancel`（单独取消）
- `schedule_only_retry`（部分失败恢复）

**不支持的情况：**
- `search → analyze → create → validate → publish`（5 步复合）
- `search(A) + search(B) → analyze(all) → create`（并行搜索）
- 条件分支（"如果搜索结果多就总结，如果少就直接用"）

### 目标

引入 **Capability** 抽象层，在 Tool 之上增加一层间接：

```
当前:  LLM → content_create_draft        (Tool 直接暴露)
目标:  LLM → GENERATE_CONTENT → content.create_draft  (Capability → Tool 映射)
```

Capability 目录（约 10-12 个）：

```python
CAPABILITIES = {
    "SEARCH_COMMUNITY":     Capability(category="SEARCH",   tool="community.search_public_posts"),
    "GET_POST_DETAIL":      Capability(category="SEARCH",   tool="community.get_post"),
    "ANALYZE_PATTERNS":     Capability(category="ANALYZE",  is_llm_step=True),  # 纯 LLM 分析
    "GENERATE_CONTENT":     Capability(category="CREATE",   tool="content.create_draft"),
    "IMPROVE_CONTENT":      Capability(category="CREATE",   tool="content.revise_draft"),
    "VALIDATE_QUALITY":     Capability(category="VALIDATE", is_llm_step=True),  # 纯 LLM 校验
    "SCHEDULE_PUBLISH":     Capability(category="PUBLISH",  tool="publication.schedule"),
    "PUBLISH_NOW":          Capability(category="PUBLISH",  tool="publication.publish_now"),
    "MANAGE_SCHEDULE":      Capability(category="PUBLISH",  tool="publication.update_schedule"),
    "CANCEL_SCHEDULE":      Capability(category="PUBLISH",  tool="publication.cancel_schedule"),
    "QUERY_ANALYTICS":      Capability(category="ANALYZE",  tool="analytics.get_post_performance"),
}
```

Planner 输出 Capability DAG（不是 Tool DAG）：

```python
CapabilityDAG(steps=[
    PlanStep(step_id="1", capability="SEARCH_COMMUNITY",  depends_on=[]),
    PlanStep(step_id="2", capability="ANALYZE_PATTERNS",  depends_on=["1"]),
    PlanStep(step_id="3", capability="GENERATE_CONTENT",  depends_on=["2"]),
    PlanStep(step_id="4", capability="VALIDATE_QUALITY",  depends_on=["3"]),
    PlanStep(step_id="5", capability="SCHEDULE_PUBLISH",  depends_on=["4"]),
])
```

Execution Engine 在运行时将 Capability 映射到具体 Tool：
- 大部分是静态映射（`SEARCH_COMMUNITY → community.search_public_posts`）
- 特殊情况动态映射（如 `SCHEDULE_PUBLISH → publication.update_schedule` 如果已有 active_schedule）

### 为什么 Capability 不是 Tool

| | Tool | Capability |
|------|------|-----------|
| 粒度 | 具体 API 调用 | 语义步骤 |
| 数量 | 16 个（会增长） | ~10 个（稳定） |
| 对 LLM | 需要知道每个工具的参数 | 只需知道能力描述 |
| 变更影响 | 新增/修改工具需更新 Planner prompt | 工具变更对 Planner 透明 |
| 纯推理步骤 | 无法表达 | `is_llm_step=True` |

---

## Gap 3：多轮任务管理 — 从 session.active_* 到 Task Registry

### 当前状态

多轮对话的任务状态全部存在 `SessionContext` 的扁平字段中：

```python
class SessionContext(BaseModel):
    active_draft_id: str | None = None       # 只能存一个
    active_post_id: str | None = None         # 只能存一个
    active_schedule_id: str | None = None      # 只能存一个
    recent_entities: list[RecentEntity] = []   # 最多 20 个，简单列表
    recent_tool_calls: list[RecentToolCall] = [] # 最多 20 个
```

多轮关联依赖 `resolve_active_draft_id()` 方法：

```python
def resolve_active_draft_id(self):
    # 1. 先看 active_draft_id（最近一次成功创建/修改的草稿）
    # 2. 否则看 recent_entities 中最近的 DRAFT
    # 3. 多个草稿时返回 None + candidates 列表
```

### 问题场景

```
轮次1: "创建一篇Java文章"          → active_draft_id = draft_123
轮次2: "修改刚才文章标题"           → resolve_active_draft_id() → draft_123 ✓
轮次3: "分析一下最近Java热门帖子"    → 搜索结果只显示给用户，不持久化
轮次4: "把分析结果加入刚才文章"     → 搜索结果已丢失 ✗
```

当前系统的 "最近实体" 只是一个按时间排序的列表，没有：
- Task 边界（搜索属于哪个 Task？）
- Artifact 关联（搜索结果如何被后续 Task 引用？）
- 依赖关系（Task B 的结果是 Task A 的输入？）
- 生命周期（Task 是进行中还是已完成？）

### 目标

引入 `Task` 作为一等概念：

```python
class Task(BaseModel):
    task_id: str
    conversation_id: str
    goal: str                          # "创建一篇Java学习文章"
    status: TaskStatus                 # PLANNING → READY → IN_PROGRESS → COMPLETED
    artifacts: list[ArtifactRef] = []  # [search_result_1, draft_789, schedule_012]
    depends_on: list[str] = []         # 依赖的其他 Task ID
    created_at: datetime
```

`TaskRegistry` 管理一个 Conversation 中的所有 Task：

```python
class TaskRegistry:
    async def create_task(intent) -> Task
    async def get_active_task(conv_id) -> Task | None
    async def resolve_task(conv_id, intent) -> Task | None  # 匹配 "刚才那个"
    async def list_tasks(conv_id) -> list[Task]
    async def add_artifact(task_id, artifact)
    async def add_dependency(task_id, depends_on_task_id)
```

多轮场景变为：

```
轮次1: "创建一篇Java文章"
    → TaskRegistry.create_task() → task_A (status=COMPLETED)
    → task_A.artifacts = [draft_123]

轮次2: "修改刚才文章标题"
    → TaskRegistry.resolve_task() → 匹配 task_A
    → TaskRegistry 内更新 task_A（MODIFY 操作）

轮次3: "分析最近Java热门帖子"
    → TaskRegistry.create_task() → task_B (status=COMPLETED)
    → task_B.artifacts = [search_result_1, analysis_report_1]

轮次4: "把分析结果加入刚才文章"
    → TaskRegistry.resolve_task() → 匹配 task_A（"刚才文章"）
    → 同时识别"分析结果" → 匹配 task_B.artifacts[1]
    → TaskRegistry.add_dependency(task_A, depends_on=task_B)
    → IMPROVE_CONTENT 时注入 task_B 的 analysis_report
```

---

## Gap 4：执行管理 — 从顺序 Tool Call 到 Execution Engine

### 当前状态

工具执行路径：

```
LLM 输出 tool_calls → routes.py tool_handler → mcp.execute_tool → handler → 返回
```

- 失败处理：返回 `ToolResult(ok=False, code=..., retryable=...)`
- agent.py 中做了调用去重（相同 (tool_name, args) 不重复执行）
- 多步骤：通过顺序展开 turn_tools 实现
- 没有 Checkpoint
- 没有 Resume

### 目标

`Execution Engine` 在 Capability DAG 的基础上增加执行期管理：

```python
class Step(BaseModel):
    step_id: str
    task_id: str
    capability: str                 # Capability 名
    status: StepStatus              # PENDING → READY → IN_PROGRESS → COMPLETED/FAILED
    tool_name: str | None           # 实际执行的 Tool
    tool_result: dict | None
    artifact: ArtifactRef | None    # 产生的中间产物
    retry_count: int
    depends_on: list[str]           # 依赖的 step_id
    checkpoint_data: dict | None    # 恢复状态

class StepStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

class ExecutionEngine:
    async def execute(task: Task) -> Task:
        """按 DAG 拓扑顺序执行 Capability，管理 Step 状态"""

    async def resume(task_id: str) -> Task:
        """从 Checkpoint 恢复执行，跳过已完成的 Step"""

    async def pause(task_id: str) -> None:
        """暂停执行，释放资源"""
```

关键设计:
- **Capability → Tool 映射** — Execution Engine 内部完成，Planner 不关心具体工具
- **纯 LLM 步骤** — 如 ANALYZE_PATTERNS、VALIDATE_QUALITY，不走 MCP，直接用 LLM 推理
- **Checkpoint** — 每个 Step 完成后持久化到 PostgreSQL，崩溃后可恢复
- **并行执行** — 无依赖的 Step 可并行（如同时搜索两个关键词）

---

# 第三部分：渐进式迁移方案

## 总览

```
Phase 1: Task Understanding      2 周    新增模块，不改变执行路径
Phase 2: Task Registry           2 周    新增模块，不改变执行路径
Phase 3: Planner                 2 周    新增路径，旧路径保留
Phase 4: Execution Engine        2 周    新增路径，旧路径保留
Phase 5: 收敛                    1 周    移除旧代码，统一路径
```

每个 Phase 结束后必须通过完整的回归测试。

## Phase 1: Task Understanding Layer

### 目标
将意图检测从关键词正则升级为 LLM 语义理解。

### 修改的文件

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `packages/assistant_core/greenbook_assistant_core/task_understanding.py` | TaskIntent 模型 + TaskUnderstanding 类 |
| **修改** | `packages/assistant_core/greenbook_assistant_core/agent.py` | `run()` 开头调用 TaskUnderstanding，结果注入 context |
| **修改** | `packages/assistant_core/greenbook_assistant_core/__init__.py` | 导出新模块 |

### 不修改的文件

- `routes.py` — 接口不变
- `services/greenbook_mcp/` — 零改动
- `context.py` — 暂不改动

### TaskIntent 模型（精简版）

```python
# packages/assistant_core/greenbook_assistant_core/task_understanding.py

class TaskIntent(BaseModel):
    """用户一轮输入的完整结构化理解"""
    relation: Literal[
        "NEW_TASK", "CONTINUE_TASK", "MODIFY_TASK",
        "QUERY_TASK", "CANCEL_TASK", "RESUME_TASK",
    ] = "NEW_TASK"

    goal: str                                    # 核心目标（一句话）
    goal_category: str                           # CREATE_CONTENT, IMPROVE_CONTENT, ...
    requirements: list[dict] = []                # [{type: "SEARCH", params: {...}}, ...]
    constraints: list[dict] = []                 # [{type: "TIME", value: "..."}, ...]
    target_task_hint: str | None = None          # 用户自然语言中的任务引用
    confidence: float = 0.0                      # 理解置信度

class TaskUnderstanding:
    """双层意图理解：快速关键词 + 深度 LLM"""

    def __init__(self, llm, model: str):
        self.llm = llm
        self.model = model

    async def understand(
        self,
        user_message: str,
        session: SessionContext,
        existing_tasks: list[dict] | None = None,
    ) -> TaskIntent:
        """主入口：先尝试快速路径，低置信度时升级到 LLM"""

        # L1: 快速路径（保留旧版关键词逻辑）
        quick = self._quick_intent(user_message, session)
        if quick.confidence > 0.8:
            return quick

        # L2: LLM 深度理解
        return await self._llm_understand(user_message, session, existing_tasks)
```

### 风险

- **低** — 只是新增了一个调用，不影响现有执行路径
- 如果 LLM 理解错误，回退到旧版关键词（安全网）
- TaskIntent 目前只用于日志和调试，不参与执行决策

---

## Phase 2: Task Registry

### 目标
引入 Task 概念和持久化，管理多任务生命周期。

### 修改的文件

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `packages/assistant_core/greenbook_assistant_core/task_registry.py` | Task 模型 + TaskRegistry 类 |
| **新增** | `packages/assistant_core/greenbook_assistant_core/db.py` | PostgreSQL 异步连接（asyncpg + SQLAlchemy） |
| **修改** | `apps/assistant_api/greenbook_assistant_api/main.py` | lifespan 中初始化 DB 连接池 |
| **修改** | `apps/assistant_api/greenbook_assistant_api/api/routes.py` | `send_message()` 中集成 TaskRegistry |

### 数据库表（新增）

```sql
CREATE TABLE assistant_tasks (
    task_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    goal TEXT NOT NULL,
    goal_category VARCHAR(64),
    status VARCHAR(32) DEFAULT 'READY',
    requirements JSONB DEFAULT '[]',
    constraints JSONB DEFAULT '[]',
    artifacts JSONB DEFAULT '[]',
    depends_on UUID[] DEFAULT '{}',
    version INT DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE assistant_artifacts (
    artifact_id UUID PRIMARY KEY,
    task_id UUID REFERENCES assistant_tasks(task_id),
    artifact_type VARCHAR(64),       -- DRAFT, SEARCH_RESULT, ANALYSIS_REPORT
    resource_id VARCHAR(128),        -- 外部资源 ID
    summary VARCHAR(500),
    content_ref JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
```

### 集成到 routes.py

在 `send_message()` 中增加（不替换现有逻辑）：

```python
# 现有代码之上新增
task_registry = request.app.state.task_registry
task_intent = await task_understanding.understand(user_message, session)
existing_tasks = await task_registry.list_tasks(conversation_id)
resolved_task = await task_registry.resolve_task(conversation_id, task_intent)

if task_intent.relation == "NEW_TASK":
    task = await task_registry.create_task(task_intent, conversation_id, user_id)
else:
    task = resolved_task  # CONTINUE / MODIFY / CANCEL / QUERY
```

### 风险

- **中** — 增加了 PostgreSQL 依赖，需要确保本地开发环境可用
- Task 表是新增的，不影响现有数据
- SessionContext 的 active_*_id 字段仍然作为快速访问缓存保留

---

## Phase 3: Planner

### 目标
将多步骤任务从硬编码的顺序工具门控升级为 LLM 驱动的 Capability DAG 规划。

### 修改的文件

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `packages/assistant_core/greenbook_assistant_core/capabilities.py` | Capability 目录定义 |
| **新增** | `packages/assistant_core/greenbook_assistant_core/planner.py` | Planner 类（LLM 驱动） |
| **修改** | `packages/assistant_core/greenbook_assistant_core/agent.py` | 复杂任务走 Planner，简单任务走旧路径 |
| **修改** | `packages/assistant_core/greenbook_assistant_core/task_registry.py` | Task 增加 plan 字段 |

### Capability 目录

```python
# packages/assistant_core/greenbook_assistant_core/capabilities.py

@dataclass
class Capability:
    name: str
    description: str
    category: str              # SEARCH, ANALYZE, CREATE, VALIDATE, PUBLISH
    default_tool: str | None   # MCP 工具名，纯 LLM 步骤为 None
    produces_artifact: bool
    artifact_type: str | None
    parallelizable: bool = False

CAPABILITIES = [
    Capability("SEARCH_COMMUNITY",   "搜索社区内容",       "SEARCH",   "community.search_public_posts", True, "SEARCH_RESULT"),
    Capability("ANALYZE_PATTERNS",   "分析内容模式和特点", "ANALYZE",  None,                            True, "ANALYSIS_REPORT"),
    Capability("GENERATE_CONTENT",   "生成新内容",         "CREATE",   "content.create_draft",          True, "DRAFT"),
    Capability("IMPROVE_CONTENT",    "改进已有内容",       "CREATE",   "content.revise_draft",          True, "DRAFT"),
    Capability("VALIDATE_QUALITY",   "校验内容质量",       "VALIDATE", None,                            True, "VALIDATION_REPORT"),
    Capability("SCHEDULE_PUBLISH",   "定时发布",           "PUBLISH",  "publication.schedule",          True, "SCHEDULE"),
    Capability("PUBLISH_NOW",        "立即发布",           "PUBLISH",  "publication.publish_now",       False, None),
    Capability("MANAGE_SCHEDULE",    "管理定时任务",       "PUBLISH",  "publication.update_schedule",   False, None),
    Capability("CANCEL_SCHEDULE",    "取消定时任务",       "PUBLISH",  "publication.cancel_schedule",   False, None),
    Capability("GET_POST_DETAIL",    "获取帖子详情",       "SEARCH",   "community.get_post",            False, None),
    Capability("QUERY_ANALYTICS",    "查询分析数据",       "ANALYZE",  "analytics.get_post_performance", False, None),
]
```

### Planner 实现

```python
# packages/assistant_core/greenbook_assistant_core/planner.py

class PlanStep(BaseModel):
    step_id: str
    capability: str
    description: str
    depends_on: list[str] = []

class CapabilityDAG(BaseModel):
    task_id: str
    steps: list[PlanStep]

class Planner:
    def __init__(self, llm, model: str):
        self.llm = llm
        self.model = model

    async def plan(
        self,
        task: Task,
        capabilities: list[Capability],
    ) -> CapabilityDAG | None:
        """LLM 生成 Capability DAG。返回 None 表示无法规划"""

        # 简单任务（单步）— 不需要 Planner
        if len(task.requirements) <= 1 and not task.constraints:
            return None  # 走旧路径

        # 复杂任务（多步）— LLM 规划
        prompt = self._build_plan_prompt(task, capabilities)
        result = await self._call_llm(prompt)
        return self._parse_plan(result)
```

### 与旧路径的并行

```
send_message()
    │
    ├── 简单任务（单步操作）
    │   → 旧路径：_turn_tool_filter + LLM 直接 tool call
    │
    └── 复杂任务（多步/有约束/有依赖）
        → 新路径：TaskUnderstanding → Planner → CapabilityDAG
        → 目前：DAG 仅记录到 task.plan，不实际执行
        → Phase 4 才由 Execution Engine 执行
```

### 风险

- **中** — Planner 输出可能不合理，需要校验
- 旧路径作为 fallback 保留
- Capability DAG 在 Phase 3 只是记录（observability），不参与执行决策

---

## Phase 4: Execution Engine

### 目标
实现 Step 状态机 + DAG 拓扑执行 + Checkpoint + 失败恢复。

### 修改的文件

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `packages/assistant_core/greenbook_assistant_core/execution_engine.py` | ExecutionEngine + Step 模型 |
| **新增** | `packages/assistant_core/greenbook_assistant_core/capability_mapper.py` | Capability → Tool 映射器 |
| **修改** | `apps/assistant_api/greenbook_assistant_api/api/routes.py` | `send_message()` 中，CapabilityDAG 任务走 Execution Engine |
| **新增** 数据库表 | `assistant_steps` | Step 持久化 |

### 核心流程

```python
class ExecutionEngine:
    async def execute(self, task: Task, mcp, llm, session) -> Task:
        dag = task.plan
        steps = self._init_steps(dag)

        while not all(s.status in (COMPLETED, FAILED, SKIPPED) for s in steps):
            ready = [s for s in steps if s.status == READY]

            # 并行执行无依赖的步骤
            parallel = [s for s in ready if s.capability.parallelizable]
            if parallel:
                await asyncio.gather(*[self._execute_step(s, task, mcp, llm, session)
                                       for s in parallel])

            # 串行执行有依赖的步骤
            sequential = [s for s in ready if not s.capability.parallelizable]
            for s in sequential:
                await self._execute_step(s, task, mcp, llm, session)

        return task

    async def _execute_step(self, step, task, mcp, llm, session):
        step.status = IN_PROGRESS
        await self._save_step(step)  # Checkpoint

        try:
            capability = get_capability(step.capability)

            if capability.default_tool is None:
                # 纯 LLM 步骤（ANALYZE_PATTERNS, VALIDATE_QUALITY）
                result = await self._execute_llm_step(step, task, llm)
            else:
                # MCP 工具步骤
                result = await mcp.execute_tool(
                    capability.default_tool,
                    auth=..., session=session, **step.tool_args,
                )

            if result.get("ok"):
                step.status = COMPLETED
                step.artifact = self._extract_artifact(step, result)
            else:
                if result.get("retryable") and step.retry_count < 3:
                    step.status = FAILED_RETRYABLE
                    step.retry_count += 1
                else:
                    step.status = FAILED

        except Exception as e:
            step.status = FAILED_RETRYABLE if step.retry_count < 3 else FAILED
            step.error_message = str(e)

        await self._save_step(step)  # Checkpoint
```

### 风险

- **高** — 这是最大的变更，涉及执行路径的实质性改变
- 简单任务仍走旧路径
- 需要充足的集成测试覆盖
- 首次上线建议在 feature flag 后灰度

---

## Phase 5: 收敛

### 目标
移除旧代码，统一到新架构。

### 操作

| 操作 | 文件 | 说明 |
|------|------|------|
| **删除** | `agent.py:_turn_intents()` | 被 TaskUnderstanding 替代 |
| **删除** | `agent.py:_turn_routing_hint()` | 被 Planner 替代 |
| **删除** | `agent.py:_turn_tool_filter()` | 被 Execution Engine 替代 |
| **删除** | `agent.py` 行 490-522 的顺序工具门控 | 被 Execution Engine DAG 执行替代 |
| **简化** | `routes.py:_build_tool_schemas()` | 改为从 tool_registry 动态生成 |
| **修改** | `context.py:SessionContext` | 移除 active_*_id 的单一值限制，改为引向 TaskRegistry |
| **迁移** | `routes.py` 的 4 个内存 store | 迁移到 PostgreSQL |

### 风险

- **低** — 此时新旧路径已验证稳定，收敛只是清理
- 每次删除前确认旧路径调用量为 0

---

## 每个 Phase 涉及的目录汇总

```
Phase 1 (Task Understanding):
    [+] packages/assistant_core/greenbook_assistant_core/task_understanding.py
    [M] packages/assistant_core/greenbook_assistant_core/agent.py
    [M] packages/assistant_core/greenbook_assistant_core/__init__.py

Phase 2 (Task Registry):
    [+] packages/assistant_core/greenbook_assistant_core/task_registry.py
    [+] packages/assistant_core/greenbook_assistant_core/db.py
    [M] apps/assistant_api/greenbook_assistant_api/main.py
    [M] apps/assistant_api/greenbook_assistant_api/api/routes.py

Phase 3 (Planner):
    [+] packages/assistant_core/greenbook_assistant_core/capabilities.py
    [+] packages/assistant_core/greenbook_assistant_core/planner.py
    [M] packages/assistant_core/greenbook_assistant_core/agent.py
    [M] packages/assistant_core/greenbook_assistant_core/task_registry.py

Phase 4 (Execution Engine):
    [+] packages/assistant_core/greenbook_assistant_core/execution_engine.py
    [+] packages/assistant_core/greenbook_assistant_core/capability_mapper.py
    [M] apps/assistant_api/greenbook_assistant_api/api/routes.py
    [+] DB migration: assistant_steps 表

Phase 5 (Convergence):
    [M] packages/assistant_core/greenbook_assistant_core/agent.py  (删除旧逻辑)
    [M] packages/assistant_core/greenbook_assistant_core/context.py (简化)
    [M] apps/assistant_api/greenbook_assistant_api/api/routes.py    (简化)

完全不修改:
    services/greenbook_mcp/          (所有文件)
    packages/java_client/            (所有文件)
    packages/creator_client/         (所有文件)
    packages/contracts/              (所有文件)
    packages/security/               (所有文件)
    packages/assistant_core/time_parser.py
    creator-agent/                   (所有文件)
```

---

## 关键设计约束

1. **不新增 Agent 角色** — 保持单 Agent 架构，不引入 Multi-Agent 层次
2. **不重写 MCP 工具** — 16 个工具的实现全部保留
3. **不修改 Java Backend** — 零改动
4. **不修改 Creator Agent** — 零改动
5. **Capability 不超过 12 个** — 保持简单，避免旧版 Capability Graph 膨胀问题
6. **每个新增模块不超过 300 行** — 保持可维护性
7. **旧路径在 Phase 5 之前始终作为 fallback** — 安全第一
