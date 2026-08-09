# GreenBook Agent Runtime 架构设计报告 v3

> 日期: 2026-08-07
> 范围: 当前轻量 Assistant Runtime → 下一代 Task-oriented Agent Runtime
> 原则: 不推翻已有稳定运行的系统，在现有基础上渐进增强

---

## 目录

1. [当前系统架构图](#1-当前系统架构图)
2. [当前 Agent 处理复杂任务能力不足分析](#2-为什么当前agent处理复杂任务能力不足)
3. [旧版 community-assistant-agent 设计评估](#3-旧版-community-assistant-agent-哪些设计值得保留)
4. [应该放弃的设计](#4-哪些设计应该放弃)
5. [新的 GreenBook Agent Runtime 架构](#5-新的-greenbook-agent-runtime-架构)
6. [Task Understanding 设计](#6-task-understanding-设计)
7. [Task Registry 设计](#7-task-registry-设计)
8. [Planner 设计](#8-planner-设计)
9. [Execution Engine 设计](#9-execution-engine-设计)
10. [数据库模型建议](#10-数据库模型建议)
11. [渐进式迁移路线](#11-渐进式迁移路线)

---

## 1. 当前系统架构图

### 1.1 当前运行链路

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Frontend (React)                              │
│                  Bearer JWT + Idempotency-Key                        │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ POST /api/v1/assistant/conversations/
                               │      {id}/messages
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  apps/assistant_api (FastAPI :8094)                   │
│                                                                      │
│  _JwtAuthMiddleware → AuthContext (user_id, tenant_id, roles)        │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    routes.py: send_message                     │   │
│  │                                                               │   │
│  │  1. Load SessionContext from in-memory conversation_store      │   │
│  │  2. Replay public message history (user/assistant only)        │   │
│  │  3. Build tool schemas (_build_tool_schemas)                  │   │
│  │  4. Construct CommunityOperationsAssistant                     │   │
│  │  5. Call assistant.run(user_message, session, tool_handler)   │   │
│  │  6. Save session + messages + run to in-memory stores         │   │
│  │  7. Return 202 RunAcceptedResponse                             │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  State stores (all in-memory dicts on app.state):                    │
│  - conversation_store  - run_store  - approval_store  - message_store│
└──────────────────────────────┬──────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│           packages/assistant_core (Agent Loop)                        │
│                                                                      │
│  CommunityOperationsAssistant.run()                                  │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  Deterministic Intent Detection (regex/keyword):               │   │
│  │  _turn_intents() → (create, revise, schedule, cancel, search) │   │
│  │  _turn_routing_hint() → "INTERNAL TURN ROUTING: ..."         │   │
│  │  _turn_tool_filter() → {allowed_tool_names}                  │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│                              ▼                                       │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  LLM Loop (max 30 rounds, temperature=0):                     │   │
│  │  messages → llm.chat.completions.create(tools, tool_choice)   │   │
│  │  → tool_calls? → tool_handler() → observation → loop          │   │
│  │  → content? → final response → break                         │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                              │                                       │
│  Sequential tool gating:                                             │
│    search → create_draft → schedule                                  │
│    revise → update_schedule                                          │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ tool_handler callback
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│         services/greenbook_mcp (In-Process MCP Adapter)               │
│                                                                      │
│  GreenBookMCPServer.execute_tool(tool_name, auth, session, ...)      │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  tool_registry lookup → Pydantic validate → ToolContext       │   │
│  │  → handler(ctx, **args) → ToolResult envelope                │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                      │
│  16 registered tools across 5 domains:                               │
│  ┌────────────┬──────────────────────────────────────────────────┐  │
│  │ community  │ search_public_posts, get_post, list_own_posts    │  │
│  │ content    │ create_draft, get_draft, list_drafts,            │  │
│  │            │ revise_draft                                     │  │
│  │ publication│ schedule, get_status, update_schedule,           │  │
│  │            │ cancel_schedule, publish_now                     │  │
│  │ interaction│ list_comments, send_reply                        │  │
│  │ analytics  │ get_post_performance, get_account_summary        │  │
│  └────────────┴──────────────────────────────────────────────────┘  │
└──────────────┬──────────────────────────────┬───────────────────────┘
               │                              │
               ▼                              ▼
┌──────────────────────────┐   ┌──────────────────────────────────────┐
│  Creator Agent (:8092)    │   │  Java Backend (:8080)                 │
│  LangGraph-based          │   │  Spring Boot                          │
│                           │   │                                       │
│  - CREATE_CONTENT task    │   │  - POST /drafts (Agent Facade)        │
│  - IMPROVE_DRAFT task     │   │  - PUT  /drafts/{id}                  │
│  - AUTO interaction mode  │   │  - POST /publish                      │
│  - Returns FinalContent   │   │  - POST /capabilities (exchange)      │
│  - RAG knowledge base     │   │  - GET  /posts/search                 │
│                           │   │  - POST /comments/replies             │
│                           │   │  - Source of truth for all data       │
└──────────────────────────┘   └──────────────────────────────────────┘
```

### 1.2 当前模块职责

| 模块 | 职责 | 行数 |
|------|------|------|
| `apps/assistant_api/routes.py` | HTTP 路由、会话管理、Run 生命周期、SSE 事件流、审批流、错误映射 | ~1240 |
| `apps/assistant_api/main.py` | App 工厂、JWT 中间件、依赖注入(lifespan)、健康检查 | ~230 |
| `packages/assistant_core/agent.py` | Agent 循环、确定性意图检测、工具筛选、路由提示、顺序工具门控 | ~544 |
| `packages/assistant_core/context.py` | SessionContext、RecentEntity、RecentToolCall、PendingApproval | ~126 |
| `packages/assistant_core/time_parser.py` | 中文相对时间确定性解析 | ~80 |
| `services/greenbook_mcp/server.py` | MCP 工具分发、Pydantic 校验、签名绑定 | ~194 |
| `services/greenbook_mcp/tool_registry.py` | 16 个工具的注册、验证、目录生成 | ~212 |
| `services/greenbook_mcp/tools/content.py` | 草稿创建/修改编排(Creator → Java Facade → GET 验证) | ~510 |
| `services/greenbook_mcp/tools/publication.py` | 发布调度/更新/取消/即时发布(状态机守卫) | ~350 |
| `services/greenbook_mcp/tools/community.py` | 社区搜索/帖子查询 | ~200 |

### 1.3 当前架构优点

1. **可稳定启动和运行** — 单进程 FastAPI，无分布式依赖，本地开发即开即用
2. **MCP 调用链清晰** — User → API → Agent Loop → MCP Adapter → Java/Creator，层次分明
3. **确定性边界明确** — 用户身份(JWT)、时间解析、参数校验、审批门控全部由代码控制，模型不可越权
4. **DeepSeek 兼容** — 处理了 dots-in-function-names、reasoning_content 回传等模型特有的兼容性问题
5. **幂等和安全设计到位** — Idempotency-Key、Capability 兑换、GET-after-write 验证、tool 失败去重
6. **部分失败语义** — revision 成功但 schedule 失败时返回 PARTIAL_FAILURE + safe_to_retry
7. **顺序工具门控** — search→create→schedule / revise→schedule 的多轮顺序由代码保证
8. **Java 作为唯一事实源** — Assistant 不直连数据库，不生成伪造结果

### 1.4 当前架构缺陷

1. **无 Task 概念** — "Run" 是瞬时的单次 LLM 调用循环，无跨轮次的 Task 抽象
2. **纯内存存储** — 进程重启丢失全部会话和 Run 状态
3. **意图识别是正则** — `_turn_intents()` 是中文关键词匹配，不理解语义：
   - "参考优秀文章优化一下"、"借鉴热门内容重新整理"、"提升文章质量" 应统一为 IMPROVE_CONTENT
   - 但关键词匹配将它们分散到了不同分支
4. **无 Planner** — 多步骤任务完全依赖 LLM 单次推理 + 代码硬编码的顺序门控
   - "搜索社区热门Java帖子，分析写作方式，生成文章，五分钟后发布" 这种 5 步任务
   - 只能通过单次模型调用 + 顺序暴露工具来近似，中间关系无法显式追踪
5. **无跨轮次任务管理** — "修改刚才文章标题" 依赖 session.active_draft_id，
   - 没有 Task Registry 来管理多任务、任务间依赖、任务状态
6. **无恢复能力** — Run 失败后状态丢失，没有 Checkpoint/Resume
7. **对话历史简单** — 仅回放 user/assistant 消息，无结构化任务上下文
8. **无长运行任务支持** — Creator 等待是同步轮询(240s deadline)，没有异步释放 Worker 的机制
9. **无并发任务** — 一个 Conversation 同一时间只能有一个活跃的 Run

---

## 2. 为什么当前 Agent 处理复杂任务能力不足

### 2.1 问题根源：从 LLM Tool Calling 到 Agent Runtime 的鸿沟

当前系统本质是：

```
User Message → Intent Keywords → LLM(tools) → Tool Execution → Response
```

这是一个 **增强型聊天机器人** 的范式，而非 **Agent Runtime** 的范式。

两者关键区别：

| 维度 | LLM Tool Calling | Agent Runtime |
|------|-----------------|---------------|
| 任务表示 | 无，单次 Run | Task 是显式数据结构，有生命周期 |
| 规划 | LLM 隐式推理 | Planner 显式输出 Capability DAG |
| 多步骤 | 顺序 tool_choice 筛选 | DAG 并行/串行编排 |
| 跨轮次 | session.active_* 字段 | Task Registry + Task State |
| 失败恢复 | HTTP 错误码 + retryable flag | Checkpoint + Resume + 补偿 |
| 意图理解 | 关键词匹配 | 语义理解 + TaskIntent 结构化输出 |
| 执行追踪 | events[] 列表 | Step 状态机 + Progress Ledger |

### 2.2 具体场景分析

**场景：** "帮我搜索社区里面热门Java帖子，分析他们的写作方式，根据分析结果生成一篇文章，标题新颖，增加代码示例，然后五分钟后发布。"

当前系统的处理路径：

```
1. _turn_intents() 检测到 "搜索" + "社区" → asks_search=True
   检测到 "生成一篇文章" → asks_create=True
   → asks_create && asks_search → tool_filter = {community_search_public_posts}

2. LLM 调用 community_search_public_posts("Java")

3. 搜索成功后，顺序门控展开 tool_filter = {content_create_draft}

4. LLM 调用 content_create_draft(title="...", instruction="...")

5. 创建成功后，model 可能返回 content（结束），
   或如果提示词中包含"发布"，则展开 tool_filter = {publication_schedule}

6. 任务完成
```

**问题：**

1. **搜索→创建之间没有"分析"步骤。** 搜索结果是作为 `references` 参数直接注入 `create_draft`，
   期望 Creator 一并完成分析和创作。但"分析写作方式"和"根据分析结果生成"是两个语义步骤，
   压缩为一个 Creator 任务丢失了中间推理。

2. **时间解析是启发式的。** "五分钟后发布" 被 `_has_future_time_expression()` 识别，
   但如果有更复杂的条件（"如果分析结果显示热门文章偏向实战，就按实战风格写"），
   无法处理。

3. **整个流程是脆弱的顺序链。** 如果搜索返回空结果，模型可能跳过分析直接创作；
   如果 Creator 返回的内容不包含代码示例，没有校验步骤。

4. **无结构化追踪。** 我们不知道"分析写作方式"这一步是否完成了、产生了什么中间产物、
   对最终创作有什么影响。

### 2.3 多轮会话场景分析

**场景：**
```
轮次1: "帮我创建一篇Java文章，明天8点发布"  → 产生任务A
轮次2: "修改刚才文章标题"                  → 应找到任务A
轮次3: "分析一下最近社区Java热门文章"       → 应创建任务B
轮次4: "把分析结果加入刚才文章"            → Task B Artifact → Task A
```

当前系统处理：

- 轮次1: 创建草稿 + 定时发布，session.active_draft_id = draft_123, session.active_schedule_id = sched_456
- 轮次2: `_turn_intents()` 检测到 "修改" → tool_filter = {content_revise_draft}，
  通过 session.active_draft_id 找到草稿 → 可以工作
- 轮次3: `_turn_intents()` 检测到 "分析" + "社区" → tool_filter = {community_search_public_posts}，
  搜索完成，但结果只是显示给用户，没有保存为结构化 Task
- 轮次4: "把分析结果加入刚才文章" — **问题来了：**
  - 关键词匹配可能触发 revise，但 model 不知道"分析结果"是什么
  - 轮次3 的搜索结果只在当时那轮有效，没有持久化为 Artifact
  - 没有 Task 概念，无法表达"Task B 的输出 → Task A 的输入"

---

## 3. 旧版 community-assistant-agent 哪些设计值得保留

### 3.1 值得保留的设计

| 设计 | 理由 | 保留方式 |
|------|------|---------|
| **TurnPlan** | 将用户自然语言消息结构化为 `TurnPlan { turn_relation, goal_ref, changes[], tasks[] }`，是一个好的"意图结构化"抽象 | 简化为 `TaskIntent`，去掉过度的 Change 类型枚举 |
| **ConversationGoal** | 跨轮次的目标持久化概念，"一个 Conversation 可以有多个 Goal" | 演化为 `Task`，增加生命周期状态机 |
| **GoalResolver** | 多 Goal 场景下的消歧匹配（序数选择、标签匹配、语义相似度） | 保留在 Task Registry 中 |
| **Artifact Contract** | 类型化的中间产物（TOOL_RESULT, POST_SEARCH_RESULTS, CONTENT_DRAFT 等），支持跨步骤流转 | 简化为通用 Artifact 模型，减少种类 |
| **TaskManager 的 ACTION 语义** | CREATE / UPDATE / CANCEL / QUERY_STATUS 的任务动作分类 | 保留在 Task Registry |
| **SideEffectLedger** | 外部写入的请求哈希、稳定操作键、执行状态追踪 | 已部分在当前 MCP 层实现(idempotency_key)，可增强 |
| **Capability Graph** | 能力 DAG 的概念（虽然实现过重） | 简化为 Planner 输出，不预先静态定义全部 DAG |
| **Run/Step 模型** | 类型化的执行追踪 | 演化为 Execution Engine 的 Step 状态机 |
| **Agent Registry** | 可发现的 Agent 能力目录 | 演化为 Capability Registry |

### 3.2 关键设计思想的保留

旧架构的核心洞见——**"模型的不确定性限制在理解与规划，状态迁移、权限、审批、幂等在确定性代码中"**——是完全正确的，应该继续贯彻。

旧架构的另一洞见——**"Turn 是 Task 的一个增量操作，不是独立的会话"**——也是正确的。当前系统将每个 Turn 当作独立的 Run 来处理，是导致多轮任务管理薄弱的根本原因。

---

## 4. 哪些设计应该放弃

| 设计 | 放弃理由 |
|------|---------|
| **Change 枚举类型爆炸** | `ChangeRole × ChangeOp` 产生了太多组合（CONTENT+CREATE, CONTENT+APPEND, CONTENT+REPLACE, SCHEDULE+UPDATE...），过度设计。用自然语言 goal + constraints 更灵活 |
| **PlanCompiler** | 将 TurnPlan 编译为 DAG 的编译器过于复杂（change_compiler.py 有 44854 行），大量编译规则难以维护 |
| **IntentDelta** | 作为 Turn 对 Goal 的增量变更抽象，概念层级过多（TurnIntent → IntentDelta → Goal → Change → Plan）。直接 TaskIntent → CapabilityDAG |
| **多 Agent 层次结构** | 旧设计有 Supervisor → AnalyticsAgent / UserInsightAgent / ContentCreationAgent / PublishAgent / SearchAgent / MCPAgent 的层次。当前单 Agent + 工具调用已经证明更简单有效 |
| **静态 Capability Graph** | 预先定义的 JSON capability_graph.json 太刚性。社区运营场景多变，应动态生成 |
| **ConversationWorkspace** | 概念与 SessionContext + Task Registry 重叠 |
| **TargetResolver / TargetBinding** | 过度泛化的目标解析。当前 `session.active_draft_id` + `resolve_active_draft_id()` 已经足够，只需增强为跨 Task 引用 |
| **TemporalResolver** | 时间解析应由 `time_parser.py`（已存在且工作良好）统一处理 |
| **GoalWorkspace** | 工作空间概念与 Task Registry 重叠 |

**核心原则：**
- 不要恢复旧代码
- 从旧设计中提取**概念**，在当前简单系统上**重新实现**
- 保持当前系统的优点：单 Agent、MCP 工具调用、确定性边界

---

## 5. 新的 GreenBook Agent Runtime 架构

### 5.1 设计原则

1. **渐进增强，不推翻重来** — Java Backend、Creator Agent、MCP 工具、Tool 调用全部保留
2. **Task 是一等公民** — Task 有 ID、状态、目标、产物、生命周期
3. **语义理解替代关键词** — LLM 驱动的意图结构化，而非正则匹配
4. **Capability 不是 Tool** — Planner 输出能力步骤，Execution Engine 映射到具体工具
5. **确定性与不确定性分离** — 理解和规划由 LLM 完成；状态迁移、权限、幂等、验证由代码完成
6. **持久化优先** — Conversation、Task、Step、Artifact 全部持久化到 PostgreSQL

### 5.2 新架构总览

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Frontend (React)                                   │
│                   Bearer JWT + Idempotency-Key                             │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │
                                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│                     apps/assistant_api (FastAPI :8094)                     │
│                                                                           │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                   Agent Runtime (NEW)                             │    │
│  │                                                                    │    │
│  │  ┌──────────────┐   ┌──────────────┐   ┌───────────────────┐    │    │
│  │  │    Task       │   │    Task      │   │    Execution      │    │    │
│  │  │ Understanding │──▶│   Registry   │──▶│    Engine         │    │    │
│  │  │    Layer      │   │              │   │                   │    │    │
│  │  └──────────────┘   └──────────────┘   └───────┬───────────┘    │    │
│  │        │                    │                   │                │    │
│  │        ▼                    ▼                   ▼                │    │
│  │  User Message     Task CRUD + State     Step State Machine      │    │
│  │  → TaskIntent     + Dependency Graph    + Retry + Checkpoint    │    │
│  │                                                                    │    │
│  │  ┌──────────────────────────────────────────────────────────┐    │    │
│  │  │                   Planner (NEW)                            │    │    │
│  │  │  Task + TaskIntent → Capability DAG                       │    │    │
│  │  │  Output: Capability Steps, not specific Tools             │    │    │
│  │  └──────────────────────────────────────────────────────────┘    │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                           │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │              Existing (保留不变)                                   │    │
│  │                                                                    │    │
│  │  SessionContext  │  CommunityOperationsAssistant                 │    │
│  │  MCP Adapter     │  Tool Registry (16 tools)                     │    │
│  │  Auth Middleware │  Time Parser                                  │    │
│  └─────────────────────────────────────────────────────────────────┘    │
└───────────────────────────────┬──────────────────────────────────────────┘
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
              Creator Agent  Java BE    PostgreSQL
              (内容创作)    (业务数据)   (Agent 状态)
```

### 5.3 新旧对比

| 组件 | 当前系统 | 新系统 |
|------|---------|--------|
| 意图识别 | 中文关键词正则 | LLM 驱动的 TaskIntent 结构化输出 |
| 任务模型 | 无，session.active_* | Task {id, goal, status, artifacts, ...} |
| 任务管理 | 无，隐含在 session | Task Registry (CRUD + 生命周期 + 依赖) |
| 规划 | LLM 隐式推理 + 代码顺序门控 | Planner 显式输出 Capability DAG |
| 执行 | 顺序 tool 调用 + events[] | Step 状态机 + Retry + Checkpoint |
| 存储 | 进程内存 dict | PostgreSQL (Task, Step, Artifact, Event) |
| 恢复 | 无 | Checkpoint → Resume |

### 5.4 关键：不改变的部分

以下全部保留，零改动：

- **Java Backend** — 用户/帖子/评论/发布状态机的唯一事实源
- **Creator Agent** — 内容创作的唯一执行者
- **MCP 工具层** — 16 个工具及其 handler 实现
- **ToolContext / AuthContext** — 身份和调用上下文的注入机制
- **安全模型** — JWT 验证、Capability 兑换、Idempotency-Key、审批门控
- **时间解析** — `time_parser.py` 确定性相对时间解析

---

## 6. Task Understanding 设计

### 6.1 概念

Task Understanding Layer 是 Agent Runtime 的入口层。它负责将用户的自然语言输入转化为结构化的 `TaskIntent`。

**输入：** 用户消息 + 会话上下文（历史 Task、当前活跃 Task、最近实体）

**输出：** `TaskIntent` — 一个结构化对象，描述用户这一轮想做什么、和哪个 Task 相关、有什么约束条件。

### 6.2 TaskIntent 模型

```python
class TaskIntent(BaseModel):
    """一轮用户输入的完整结构化理解"""

    # 本轮与 Task 的关系
    relation: Literal[
        "NEW_TASK",         # 创建全新任务
        "CONTINUE_TASK",    # 继续已有任务（添加子步骤）
        "MODIFY_TASK",      # 修改已有任务的目标/参数
        "QUERY_TASK",       # 查询任务状态
        "CANCEL_TASK",      # 取消任务
        "RESUME_TASK",      # 恢复暂停的任务
    ]

    # 目标任务引用（当 relation != NEW_TASK 时）
    target_task_id: str | None = None
    target_task_hint: str | None = None  # 用户描述中的任务引用（如"刚才那篇"）

    # 核心目标（一句话精炼）
    goal: str

    # 目标分类
    goal_category: Literal[
        "CREATE_CONTENT",
        "IMPROVE_CONTENT",
        "ANALYZE_COMMUNITY",
        "PUBLISH_CONTENT",
        "MANAGE_SCHEDULE",
        "INTERACT_WITH_USERS",
        "QUERY_INFORMATION",
        "MULTI_STEP_COMPOSITE",
    ]

    # 结构化需求
    requirements: list[Requirement] = []
    # 例如:
    # Requirement(type="SEARCH", params={"topic": "Java", "sort": "hot"})
    # Requirement(type="ANALYZE", params={"aspect": "writing_style"})
    # Requirement(type="CREATE", params={"format": "POST", "tone": "PRACTICAL"})
    # Requirement(type="SCHEDULE", params={"run_at": "2026-08-08T08:00:00+08:00"})

    # 约束条件
    constraints: list[Constraint] = []
    # 例如: Constraint(type="TIME", value="after 5 minutes")
    #       Constraint(type="REFERENCE", value="community_hot_posts")
    #       Constraint(type="STYLE", value="include_code_examples")

    # 显式引用（用户消息中明确提到的实体）
    explicit_refs: list[EntityRef] = []
    # 例如: EntityRef(kind="DRAFT", id="draft_123", label="那篇Java文章")

    # 会话上下文中的隐式引用
    implicit_refs: list[EntityRef] = []

    # 预期输出
    expected_output: OutputExpectation | None = None
    # 例如: OutputExpectation(format="POST", publish=True, schedule_at="...")

class Requirement(BaseModel):
    type: str          # SEARCH, ANALYZE, CREATE, VALIDATE, PUBLISH, REPLY, ...
    params: dict[str, Any] = {}

class Constraint(BaseModel):
    type: str          # TIME, REFERENCE, STYLE, AUDIENCE, LENGTH, FORMAT, ...
    value: Any

class EntityRef(BaseModel):
    kind: str          # DRAFT, POST, SCHEDULE, TASK, ARTIFACT
    id: str | None     # 如果有明确ID
    label: str | None  # 用户描述中的引用标签

class OutputExpectation(BaseModel):
    format: str = "POST"
    publish: bool = False
    schedule_at: str | None = None
```

### 6.3 语义意图统一

```
"参考优秀文章优化一下"      ─┐
"借鉴热门内容重新整理"      ─┤
"提升文章质量"              ─┼── 统一为 IMPROVE_CONTENT
"让文章更有吸引力"          ─┤
"按照社区热门写法改写"      ─┘

"帮我分析社区Java帖子"      ─┐
"看看最近热门内容有什么特点" ─┼── 统一为 ANALYZE_COMMUNITY
"总结一下趋势"              ─┘
```

**实现方式：** 不是更多的正则，而是用 LLM 做语义理解。

```
User Message + Conversation Context
    ↓
LLM Prompt (Task Understanding)
    ↓
TaskIntent (Pydantic 结构化输出)
```

关键 Prompt 设计：
- 提供 goal_category 的语义定义（不是关键词列表）
- 提供 relation 的判断规则（基于对话历史和已有 Task）
- 要求模型从会话上下文中解析隐式引用
- 约束输出为严格的 JSON Schema（Pydantic 校验）

### 6.4 Task Understanding 的双层实现

参考 Magentic-One 的 Task Ledger 思想，Task Understanding 分两层：

**L1 - 快速分类（确定性）:**
- 保留当前的 `_turn_intents()` 作为快速路径
- 对于明显的单步操作（如 "列出我的草稿"），直接路由，不走 LLM
- 当快速分类置信度低或检测到复合意图时，升级到 L2

**L2 - 深度理解（LLM）:**
- LLM 分析完整语义，输出结构化 TaskIntent
- 使用 Pydantic 校验保证输出格式
- 缓存理解结果到 Task Record

---

## 7. Task Registry 设计

### 7.1 概念

Task Registry 是 Agent Runtime 的中枢。它管理所有 Task 的生命周期，提供跨轮次的 Task 查询、更新、依赖管理。

**核心职责：**
1. Task CRUD
2. Task 状态机管理
3. Task 间依赖追踪
4. 多 Task 消歧（当用户说"刚才那个"时）
5. Task 与 Conversation 的关联

### 7.2 Task 模型

```python
class Task(BaseModel):
    """一个长期目标，可能跨多个 Turn 和多轮对话"""

    task_id: str                          # UUID
    conversation_id: str                  # 所属会话
    user_id: str                          # frozen from AuthContext
    tenant_id: str                        # frozen from AuthContext

    # 目标描述
    goal: str                            # 自然语言目标
    goal_category: str                   # CREATE_CONTENT, ANALYZE_COMMUNITY, ...
    goal_summary: str | None             # 一句话摘要，用于列表展示

    # 状态
    status: TaskStatus                   # 生命周期状态
    phase: str | None                    # 当前阶段（如 "SEARCHING", "CREATING", "WAITING_SCHEDULE"）

    # 结构化需求
    requirements: list[Requirement] = []  # 从 TaskIntent 继承
    constraints: list[Constraint] = []    # 从 TaskIntent 继承

    # 产物
    artifacts: list[ArtifactRef] = []     # 这个 Task 产生的所有 Artifact

    # 依赖
    parent_task_id: str | None = None     # 父 Task（如果这是一个子任务）
    depends_on: list[str] = []            # 依赖的其他 Task ID

    # 计划
    plan: CapabilityDAG | None = None     # Planner 输出的执行计划

    # 执行追踪
    current_step_index: int = 0
    total_steps: int = 0
    last_error: str | None = None
    retry_count: int = 0

    # 时间
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    # 版本
    version: int = 1                     # 乐观锁

class TaskStatus(str, Enum):
    PLANNING = "PLANNING"                # 正在规划中
    READY = "READY"                      # 规划完成，等待执行
    IN_PROGRESS = "IN_PROGRESS"          # 执行中
    WAITING_APPROVAL = "WAITING_APPROVAL" # 等待用户审批
    WAITING_DEPENDENCY = "WAITING_DEPENDENCY" # 等待依赖 Task 完成
    PAUSED = "PAUSED"                    # 用户暂停
    COMPLETED = "COMPLETED"              # 成功完成
    PARTIAL_COMPLETED = "PARTIAL_COMPLETED" # 部分完成（某些步骤失败）
    FAILED = "FAILED"                    # 失败
    CANCELLED = "CANCELLED"              # 取消

class ArtifactRef(BaseModel):
    artifact_id: str
    artifact_type: str                  # DRAFT, SEARCH_RESULT, ANALYSIS_REPORT, ...
    created_by_step: str                # 哪个 Step 产生的
    resource_id: str | None             # 外部资源 ID（如 draft_id）
    summary: str | None                 # 简短摘要
    created_at: datetime
```

### 7.3 Task Registry 操作

```python
class TaskRegistry:
    """管理 Conversation 范围内的所有 Task"""

    # ── CRUD ──

    async def create_task(
        self,
        intent: TaskIntent,
        conversation_id: str,
        user_id: str,
    ) -> Task:
        """从 TaskIntent 创建新 Task"""

    async def get_task(self, task_id: str) -> Task | None:
        """获取单个 Task"""

    async def update_task(self, task: Task) -> Task:
        """更新 Task（乐观锁）"""

    async def delete_task(self, task_id: str) -> None:
        """软删除 Task"""

    # ── 查询 ──

    async def list_tasks(
        self,
        conversation_id: str,
        status: TaskStatus | None = None,
    ) -> list[Task]:
        """列出会话的所有 Task"""

    async def get_active_task(self, conversation_id: str) -> Task | None:
        """获取当前活跃 Task（IN_PROGRESS 或 READY）"""

    async def resolve_task(
        self,
        conversation_id: str,
        intent: TaskIntent,
    ) -> Task | None:
        """根据 TaskIntent 推断目标任务
        - 如果 intent.relation == NEW_TASK，返回 None（将创建新 Task）
        - 如果 intent.relation == CONTINUE_TASK / MODIFY_TASK，匹配已有 Task
        - 匹配策略：target_task_id > target_task_hint > 最近活跃 Task
        """

    # ── 状态转换 ──

    async def transition_status(
        self,
        task_id: str,
        new_status: TaskStatus,
        reason: str = "",
    ) -> Task:
        """Task 状态机转换，校验合法性"""

    # ── 依赖管理 ──

    async def add_dependency(
        self,
        task_id: str,
        depends_on_task_id: str,
    ) -> None:
        """添加 Task 间依赖"""

    async def get_dependent_tasks(self, task_id: str) -> list[Task]:
        """获取依赖此 Task 的其他 Task"""

    # ── Artifact ──

    async def add_artifact(
        self,
        task_id: str,
        artifact: ArtifactRef,
    ) -> None:
        """将 Artifact 关联到 Task"""

    async def get_artifacts(self, task_id: str) -> list[ArtifactRef]:
        """获取 Task 的所有产物"""
```

### 7.4 Task 状态机

```
                    ┌─────────────┐
                    │  PLANNING   │
                    └──────┬──────┘
                           │ plan ready
                           ▼
                    ┌─────────────┐
        ┌───────────│    READY    │◄──────────┐
        │           └──────┬──────┘           │
        │                  │ start             │ resume
        │                  ▼                   │
        │           ┌─────────────┐           │
        │   ┌───────│ IN_PROGRESS ├───────┐   │
        │   │       └──────┬──────┘       │   │
        │   │              │              │   │
        │   │    ┌─────────┼─────────┐    │   │
        │   │    ▼         ▼         ▼    │   │
        │   │ ┌──────┐ ┌────────┐ ┌────┐ │   │
        │   │ │PAUSED│ │APPROVAL│ │WAIT│ │   │
        │   │ └──┬───┘ └───┬────┘ │DEP │ │   │
        │   │    │         │      └──┬─┘ │   │
        │   │    │    ┌────┘         │   │   │
        │   │    │    ▼              ▼   │   │
        │   │    │  ┌──────────┐ ┌──────┐ │   │
        │   │    └─▶│COMPLETED │ │FAILED│ │   │
        │   │       └──────────┘ └──┬───┘ │   │
        │   │                       │     │   │
        │   │                 ┌─────┘     │   │
        │   │                 ▼           │   │
        │   │          ┌───────────┐      │   │
        │   └─────────▶│ CANCELLED │◄─────┘   │
        │              └───────────┘          │
        │                                     │
        └─────────────────────────────────────┘
```

### 7.5 Task 间关系示例

```
Conversation: "帮我运营Java专区"

Task A: ANALYZE_COMMUNITY           Task B: ANALYZE_USERS
"分析Java专区热门帖子写作方式"       "分析Java专区活跃用户画像"
status: COMPLETED                   status: COMPLETED
artifacts: [analysis_report_1]      artifacts: [user_profile_1]
        │                                    │
        └────────────┬───────────────────────┘
                     │ depends_on: [task_a, task_b]
                     ▼
              Task C: CREATE_CONTENT
              "根据分析结果创作一篇Java实战文章"
              status: IN_PROGRESS
              artifacts: [draft_789]
                     │
                     ▼
              Task D: PUBLISH_CONTENT
              "五分钟后发布"
              depends_on: [task_c]
              status: READY
```

---

## 8. Planner 设计

### 8.1 核心思想：Capability > Tool

当前系统的问题是将 Tool 直接暴露给 LLM：

```
User → LLM → community_search_public_posts → LLM → content_create_draft → ...
```

Planner 的职责是在 Task 和 Tool 之间增加一层抽象 —— **Capability**：

```
Task → Planner → Capability DAG → Execution Engine → Tool Mapping → MCP Tools
```

**Capability 是"需要做什么"，Tool 是"用什么做"。**

例如，对于 "CREATE_CONTENT_WITH_RESEARCH" 这个 Task：

```
Planner 输出 Capability DAG:

    SEARCH_COMMUNITY ──┐
                       ├── ANALYZE_PATTERNS ──► GENERATE_CONTENT ──► VALIDATE_QUALITY ──► SCHEDULE_PUBLISH
    QUERY_OWN_DRAFTS ──┘

Execution Engine 将 Capability 映射到 Tool:

    SEARCH_COMMUNITY   → community.search_public_posts
    ANALYZE_PATTERNS   → (LLM 分析，无工具调用，产生中间 Artifact)
    GENERATE_CONTENT   → content.create_draft (with analysis as references)
    VALIDATE_QUALITY   → (LLM 校验，检查代码示例、标题新颖性等)
    SCHEDULE_PUBLISH   → publication.schedule
```

### 8.2 Capability 目录

```python
class Capability(BaseModel):
    """一个可执行的能力单元"""

    name: str                          # SEARCH_COMMUNITY, GENERATE_CONTENT, ...
    description: str                   # 一句话描述
    category: str                      # SEARCH, ANALYZE, CREATE, VALIDATE, PUBLISH, INTERACT
    requires_tool: bool = True         # 是否需要调用 MCP Tool
    default_tool: str | None = None    # 默认映射的 Tool 名称
    produces_artifact: bool = False    # 是否产生中间产物
    artifact_type: str | None = None   # 产物类型
    is_llm_step: bool = False          # 是否为纯 LLM 推理步骤（不调用外部工具）
    parallelizable: bool = False       # 是否可以与其他 Capability 并行

CAPABILITY_CATALOG = {
    "SEARCH_COMMUNITY": Capability(
        name="SEARCH_COMMUNITY",
        description="搜索社区内容",
        category="SEARCH",
        default_tool="community.search_public_posts",
        produces_artifact=True,
        artifact_type="SEARCH_RESULT",
    ),
    "GET_POST_DETAIL": Capability(
        name="GET_POST_DETAIL",
        description="获取帖子详情",
        category="SEARCH",
        default_tool="community.get_post",
    ),
    "ANALYZE_PATTERNS": Capability(
        name="ANALYZE_PATTERNS",
        description="分析内容模式和写作特点",
        category="ANALYZE",
        requires_tool=False,
        is_llm_step=True,
        produces_artifact=True,
        artifact_type="ANALYSIS_REPORT",
    ),
    "GENERATE_CONTENT": Capability(
        name="GENERATE_CONTENT",
        description="生成内容（通过 Creator Agent）",
        category="CREATE",
        default_tool="content.create_draft",
        produces_artifact=True,
        artifact_type="DRAFT",
    ),
    "IMPROVE_CONTENT": Capability(
        name="IMPROVE_CONTENT",
        description="改进已有内容",
        category="CREATE",
        default_tool="content.revise_draft",
        produces_artifact=True,
        artifact_type="DRAFT",
    ),
    "VALIDATE_QUALITY": Capability(
        name="VALIDATE_QUALITY",
        description="校验内容质量",
        category="VALIDATE",
        requires_tool=False,
        is_llm_step=True,
        produces_artifact=True,
        artifact_type="VALIDATION_REPORT",
    ),
    "SCHEDULE_PUBLISH": Capability(
        name="SCHEDULE_PUBLISH",
        description="定时发布",
        category="PUBLISH",
        default_tool="publication.schedule",
        produces_artifact=True,
        artifact_type="SCHEDULE",
    ),
    # ... 更多
}
```

### 8.3 Planner 输入/输出

```python
class PlanStep(BaseModel):
    """Capability DAG 中的一个步骤"""
    step_id: str
    capability: str                    # Capability 名称
    description: str                   # 这一步具体做什么（自然语言）
    depends_on: list[str] = []         # 依赖的 step_id 列表
    input_artifacts: list[str] = []    # 需要的输入 Artifact（来自上游步骤）
    constraints: dict[str, Any] = {}   # 步骤级约束
    parallelizable: bool = False

class CapabilityDAG(BaseModel):
    """Planner 输出的执行计划"""
    task_id: str
    steps: list[PlanStep]
    plan_summary: str                  # 计划的自然语言概述
    generated_at: datetime

class Planner:
    """将 Task + TaskIntent 转换为 Capability DAG"""

    async def plan(
        self,
        task: Task,
        intent: TaskIntent,
        available_capabilities: list[Capability],
        conversation_context: list[Task],  # 会话中其他 Task
    ) -> CapabilityDAG:
        """
        使用 LLM 生成执行计划。

        Prompt 包含:
        - Task 的 goal 和 requirements
        - 所有可用 Capability 及其描述
        - 会话中已有 Task 及其产物（可作为输入）
        - 预期输出格式
        - 输出必须为 PlanStep 的 JSON 数组
        """
```

### 8.4 Planner 的 LLM Prompt 设计要点

```
你是一个社区运营任务的规划器。

## 当前任务
目标: {task.goal}
需求: {task.requirements}
约束: {task.constraints}

## 可用能力
{capability_catalog}

## 会话已有任务及产物
{existing_tasks_context}

## 规划规则
1. SEARCH 和 ANALYZE 必须在 CREATE 之前
2. 可以并行的步骤标记 parallelizable=true
3. 每个步骤明确需要的输入 Artifact
4. VALIDATE 步骤在 PUBLISH 之前
5. 如果用户没有要求发布，不要添加 PUBLISH 步骤
6. 步骤数量限制在 3-8 个

## 输出格式
{plan_step_schema}
```

### 8.5 从 Plan 到 Tool 的映射

Planner 输出 Capability DAG 后，Execution Engine 在运行时将 Capability 映射到具体 Tool：

```python
class CapabilityToolMapper:
    """将 Capability 映射到具体的 MCP Tool"""

    def map(self, capability: str, context: TaskContext) -> ToolCall:
        """
        大部分 Capability 有默认 Tool 映射。
        特殊情况下可以动态选择：
        - 如果用户有 active_schedule_id，SCHEDULE_PUBLISH 映射到 publication.update_schedule
        - 如果用户有 active_draft_id，GENERATE_CONTENT 可能映射到 content.revise_draft
        """
```

---

## 9. Execution Engine 设计

### 9.1 核心职责

Execution Engine 负责将 Plan (Capability DAG) 一步步执行：

1. **Step 调度** — 按 DAG 拓扑顺序执行步骤，并行步骤可同时执行
2. **Step 状态管理** — 每个 Step 有独立状态机
3. **重试** — 可重试的失败自动重试
4. **Checkpoint** — 每步完成后持久化状态
5. **Resume** — 从 Checkpoint 恢复执行
6. **失败恢复** — 区分可恢复和不可恢复的失败

### 9.2 Step 状态机

```python
class StepStatus(str, Enum):
    PENDING = "PENDING"                # 等待依赖完成
    READY = "READY"                    # 依赖已满足，可以执行
    IN_PROGRESS = "IN_PROGRESS"        # 执行中
    COMPLETED = "COMPLETED"            # 成功完成
    FAILED_RETRYABLE = "FAILED_RETRYABLE"  # 失败但可重试
    FAILED = "FAILED"                  # 不可恢复的失败
    SKIPPED = "SKIPPED"                # 因上游失败而跳过

class Step(BaseModel):
    step_id: str
    task_id: str
    plan_step: PlanStep
    status: StepStatus
    capability: str
    tool_name: str | None              # 实际执行的 Tool
    tool_args: dict[str, Any] | None
    tool_result: dict[str, Any] | None
    artifact: ArtifactRef | None       # 产生的中间产物
    started_at: datetime | None
    completed_at: datetime | None
    retry_count: int = 0
    max_retries: int = 3
    error_message: str | None = None
    checkpoint_data: dict[str, Any] | None  # 恢复所需的最小状态
```

状态转换：

```
PENDING ──► READY ──► IN_PROGRESS ──► COMPLETED
                           │
                           ├──► FAILED_RETRYABLE ──► READY (retry)
                           │
                           └──► FAILED
                                    │
                                    ▼
                           下游 Step → SKIPPED
```

### 9.3 Execution Engine 核心逻辑

```python
class ExecutionEngine:
    """执行 Capability DAG，管理 Step 生命周期"""

    def __init__(
        self,
        mcp: GreenBookMCPServer,
        llm: AsyncOpenAI,
        capability_mapper: CapabilityToolMapper,
        db: AsyncSession,               # PostgreSQL session
    ):
        ...

    async def execute(self, task: Task) -> Task:
        """执行一个 Task 的 Plan"""

        dag = task.plan
        if not dag:
            raise ValueError("Task has no plan")

        # 初始化所有 Step
        steps = self._initialize_steps(task, dag)

        # 拓扑排序 + 并行执行
        while not self._is_complete(steps):
            # 找到所有 READY 的 Step
            ready_steps = [s for s in steps if s.status == StepStatus.READY]

            # 并行执行可以并行的 Step
            parallel = [s for s in ready_steps if s.plan_step.parallelizable]
            sequential = [s for s in ready_steps if not s.plan_step.parallelizable]

            if parallel:
                await asyncio.gather(*[self._execute_step(s, task) for s in parallel])

            if sequential:
                for s in sequential:
                    await self._execute_step(s, task)

            # 检查是否需要暂停（等待审批、依赖等）
            if any(s.status == StepStatus.FAILED for s in steps):
                break

        return task

    async def _execute_step(self, step: Step, task: Task) -> Step:
        """执行单个 Step"""

        step.status = StepStatus.IN_PROGRESS
        step.started_at = datetime.now(UTC)
        await self._save_step(step)

        try:
            # 纯 LLM 步骤（如 ANALYZE_PATTERNS, VALIDATE_QUALITY）
            if step.capability in LLM_ONLY_CAPABILITIES:
                result = await self._execute_llm_step(step, task)
            else:
                # 映射 Capability → Tool
                tool_name, tool_args = self.capability_mapper.map(
                    step.capability,
                    context=TaskContext(task=task, step=step),
                )
                step.tool_name = tool_name
                step.tool_args = tool_args

                # 执行 Tool（通过现有 MCP）
                result = await self.mcp.execute_tool(
                    tool_name,
                    auth=...,     # 从 task 获取
                    session=...,  # 从 conversation 获取
                    **tool_args,
                )

            if result.get("ok"):
                step.status = StepStatus.COMPLETED
                step.tool_result = result
                # 保存中间产物
                if artifact := self._extract_artifact(step, result):
                    step.artifact = artifact
                    await self._save_artifact(task, artifact)
            else:
                if result.get("retryable") and step.retry_count < step.max_retries:
                    step.status = StepStatus.FAILED_RETRYABLE
                    step.retry_count += 1
                else:
                    step.status = StepStatus.FAILED
                step.error_message = result.get("user_message")

        except Exception as e:
            if step.retry_count < step.max_retries:
                step.status = StepStatus.FAILED_RETRYABLE
                step.retry_count += 1
            else:
                step.status = StepStatus.FAILED
            step.error_message = str(e)

        step.completed_at = datetime.now(UTC)
        await self._save_step(step)           # Checkpoint
        await self._propagate_to_downstream(step, steps)

        return step
```

### 9.4 Checkpoint 与 Resume

```python
async def resume_task(self, task_id: str) -> Task:
    """从 Checkpoint 恢复 Task 执行"""

    task = await self.db.get(Task, task_id)
    steps = await self.db.execute(
        select(Step).where(Step.task_id == task_id).order_by(Step.started_at)
    )

    # 已完成的 Step 不重复执行
    completed_step_ids = {s.step_id for s in steps if s.status == StepStatus.COMPLETED}

    # 恢复或重试失败的 Step
    for step in steps:
        if step.step_id in completed_step_ids:
            continue
        if step.status == StepStatus.FAILED_RETRYABLE:
            step.status = StepStatus.READY
        elif step.status == StepStatus.IN_PROGRESS:
            # 进程在上次执行中崩溃，通过幂等重放恢复
            step.status = StepStatus.READY

    task.status = TaskStatus.IN_PROGRESS
    await self.db.commit()

    return await self.execute(task)
```

### 9.5 与现有 Tool/MCP 的集成

Execution Engine **不替代** 现有的 MCP 工具层。它是在其之上增加编排能力：

```
Execution Engine
    │
    │ Capability → Tool 映射
    ▼
GreenBookMCPServer.execute_tool()  ← 现有，不变
    │
    │ ToolContext(auth, session, java, creator, ...)
    ▼
Tool Handler (content.py, publication.py, ...) ← 现有，不变
    │
    ▼
Java Backend / Creator Agent  ← 现有，不变
```

### 9.6 参考 Magentic-One 的 Progress Ledger

在每一步执行前后，Execution Engine 维护一个轻量的 Progress Ledger：

```python
class ProgressLedger:
    """追踪当前 Task 的执行进度"""

    task_id: str
    total_steps: int
    completed_steps: int
    current_step: str | None
    stall_counter: int = 0
    max_stalls: int = 3

    def check_progress(self) -> bool:
        """检查是否在推进，更新 stall counter"""
        ...

    def is_stalled(self) -> bool:
        """连续 stall 达到上限 → 触发重规划"""
        return self.stall_counter >= self.max_stalls
```

当 Progress Ledger 检测到 stall（连续重试/循环），Execution Engine 会触发 Plan Revision：
回到 Planner，基于当前已完成步骤和最新 Task 状态，重新规划剩余步骤。

---

## 10. 数据库模型建议

### 10.1 PostgreSQL 表设计

在现有 `assistant_*` 命名空间下新增表：

```sql
-- Task 表：跨轮次的长期目标
CREATE TABLE assistant_tasks (
    task_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL,
    user_id VARCHAR(64) NOT NULL,
    tenant_id VARCHAR(64) NOT NULL,
    goal TEXT NOT NULL,
    goal_category VARCHAR(64) NOT NULL,
    goal_summary VARCHAR(500),
    status VARCHAR(32) NOT NULL DEFAULT 'PLANNING',
    phase VARCHAR(64),
    requirements JSONB DEFAULT '[]',
    constraints JSONB DEFAULT '[]',
    plan JSONB,                          -- CapabilityDAG 序列化
    parent_task_id UUID,
    depends_on UUID[] DEFAULT '{}',
    current_step_index INT DEFAULT 0,
    total_steps INT DEFAULT 0,
    last_error TEXT,
    retry_count INT DEFAULT 0,
    version INT DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    FOREIGN KEY (conversation_id) REFERENCES assistant_conversations(conversation_id)
);

CREATE INDEX idx_tasks_conversation ON assistant_tasks(conversation_id, status);
CREATE INDEX idx_tasks_user ON assistant_tasks(user_id, tenant_id);

-- Step 表：单个执行步骤
CREATE TABLE assistant_steps (
    step_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES assistant_tasks(task_id),
    capability VARCHAR(64) NOT NULL,
    description TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    depends_on UUID[] DEFAULT '{}',
    input_artifacts UUID[] DEFAULT '{}',
    tool_name VARCHAR(128),
    tool_args JSONB,
    tool_result JSONB,
    artifact_id UUID,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    retry_count INT DEFAULT 0,
    max_retries INT DEFAULT 3,
    error_message TEXT,
    checkpoint_data JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_steps_task ON assistant_steps(task_id);

-- Artifact 表：中间产物
CREATE TABLE assistant_artifacts (
    artifact_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES assistant_tasks(task_id),
    step_id UUID NOT NULL REFERENCES assistant_steps(step_id),
    artifact_type VARCHAR(64) NOT NULL,   -- SEARCH_RESULT, DRAFT, ANALYSIS_REPORT, ...
    resource_id VARCHAR(128),             -- 外部资源 ID（如 draft_id）
    resource_kind VARCHAR(32),            -- DRAFT, POST, SCHEDULE
    summary VARCHAR(500),
    content_ref TEXT,                     -- JSON 引用 or 摘要（不存完整内容）
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_artifacts_task ON assistant_artifacts(task_id);

-- TaskIntent 表：审计追踪
CREATE TABLE assistant_task_intents (
    intent_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL,
    task_id UUID REFERENCES assistant_tasks(task_id),
    run_id UUID REFERENCES assistant_runs(run_id),
    relation VARCHAR(32) NOT NULL,
    goal TEXT NOT NULL,
    goal_category VARCHAR(64),
    intent_json JSONB NOT NULL,           -- 完整 TaskIntent JSON
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 10.2 与现有表的关系

```
assistant_conversations ──┬── assistant_messages (现有)
                          │
                          ├── assistant_runs (现有，增强)
                          │     └── task_id (新增 FK)
                          │
                          ├── assistant_tasks (新增)
                          │     ├── assistant_steps (新增)
                          │     │     └── assistant_artifacts (新增)
                          │     └── assistant_task_intents (新增)
                          │
                          └── assistant_scheduled_actions (现有)
```

### 10.3 为什么选择 PostgreSQL 而非纯内存

1. **跨轮次持久化** — 用户在 Conversation 中创建 Task，下次登录后 Task 仍在
2. **恢复能力** — Worker 崩溃后从 Checkpoint 恢复
3. **多实例** — 如果将来需要多 Worker，PostgreSQL advisory lock 提供分布式协调
4. **审计** — 所有 TaskIntent、Step 执行记录可追溯
5. **与现有 DOCS/COMMUNITY_ASSISTANT.md 设计对齐** — 文档中已规划 PostgreSQL 作为状态存储

---

## 11. 渐进式迁移路线

### Phase 0: 基础设施准备（1 周）

**目标：** 建立持久化基础，不影响现有功能。

1. 创建 `assistant_tasks`, `assistant_steps`, `assistant_artifacts`, `assistant_task_intents` 表
2. 在 `apps/assistant_api/main.py` lifespan 中初始化 PostgreSQL 连接池
3. 将现有的 `conversation_store`, `run_store`, `message_store` 从 dict 迁移到 PostgreSQL
4. 保持 API 接口不变，仅改变存储后端
5. 验证：现有 E2E 测试全部通过

### Phase 1: Task Understanding Layer（1-2 周）

**目标：** 引入语义意图理解，但不改变执行路径。

1. 实现 `TaskIntent` Pydantic 模型
2. 实现 Task Understanding LLM Prompt（作为 agent.py 中 `_turn_intents()` 的补充，而非替代）
3. 双路实现：
   - 简单明显的操作 → 继续使用确定性 `_turn_intents()`（快速路径）
   - 复杂/模糊/复合意图 → 调用 LLM 生成 TaskIntent（深度路径）
4. 将生成的 TaskIntent 持久化到 `assistant_task_intents` 表
5. 验证：复杂语义场景的意图识别准确率提升

### Phase 2: Task Registry（2 周）

**目标：** 引入 Task 概念，支持多任务管理。

1. 实现 `Task` 模型和 `TaskRegistry`
2. 在 `routes.py` `send_message` 中集成 TaskRegistry：
   - 每轮输入先经过 TaskRegistry.resolve_task()
   - NEW_TASK → 创建 Task
   - CONTINUE_TASK → 找到已有 Task 继续
3. 扩充 `SessionContext`：`active_task_id` 取代分散的 `active_*_id`
4. 新增 API：
   - `GET /conversations/{id}/tasks` — 列出会话的 Task
   - `GET /tasks/{id}` — Task 详情（含 Step、Artifact）
   - `POST /tasks/{id}/cancel` — 取消 Task
5. 验证：多轮多任务场景（创建→修改→新任务→跨任务引用）

### Phase 3: Planner（2 周）

**目标：** 引入 Capability DAG 规划能力。

1. 实现 `Capability` 目录
2. 实现 `Planner.plan()` — LLM 驱动的规划
3. 在 Task 进入 READY 状态前，Planner 生成 CapabilityDAG
4. 将 Plan 持久化到 `task.plan` 字段
5. 保留当前的顺序工具门控作为 fallback（简单任务不需要完整规划）
6. 验证：复杂任务的 DAG 生成质量（搜索→分析→创作→校验→发布）

### Phase 4: Execution Engine（2-3 周）

**目标：** 实现 Step 状态机、重试、Checkpoint、Resume。

1. 实现 `ExecutionEngine` 核心循环
2. 实现 `CapabilityToolMapper`
3. 实现 `ProgressLedger` 和 stall detection
4. 集成到 `routes.py`：Planner 输出 → Execution Engine 执行
5. 保留当前简单路径作为 DIRECT 模式（单步操作不走 Execution Engine）
6. 实现 Checkpoint/Resume 机制
7. 新增 API：
   - `POST /tasks/{id}/pause` — 暂停任务
   - `POST /tasks/{id}/resume` — 恢复任务
   - `GET /tasks/{id}/steps` — 查看步骤详情
8. 验证：任务暂停/恢复、失败重试、崩溃恢复

### Phase 5: 收敛与优化（1-2 周）

**目标：** 清理旧代码，统一接口。

1. 移除 `agent.py` 中的确定性意图检测（保留为 Task Understanding 的快速路径）
2. 移除 `agent.py` 中的顺序工具门控逻辑（Planner 替代）
3. 统一 Run 和 Task 的概念（Run 成为 Task 的一次执行尝试）
4. 性能优化：LLM 调用批量化、缓存 Plan
5. 完整回归测试

### 渐进式迁移总览

```
Phase 0:     PostgreSQL 存储 ──────────── 零功能变更
Phase 1:     TaskIntent 生成 ──────────── 内部增强，外部无感
Phase 2:     Task 概念 ─────────────────── 新增 API，向后兼容
Phase 3:     Planner ──────────────────── 复杂任务走新路径，简单任务走旧路径
Phase 4:     Execution Engine ──────────── 同上，双轨运行
Phase 5:     收敛 ──────────────────────── 移除旧代码，统一到新架构

每个 Phase 结束后，完整回归测试套件必须通过。
```

---

## 总结

本方案的核心思想是：**在当前稳定运行的轻量系统上，增加一个 Task-oriented Layer**。

不推翻已有系统，不重写工具层，不改变 Java/Creator/MCP 的调用链。

增加的能力：
1. **Task Understanding** — 语义意图理解替代关键词匹配
2. **Task Registry** — 多任务生命周期管理和跨轮次追踪
3. **Planner** — 从 Task 到 Capability DAG 的结构化规划
4. **Execution Engine** — Step 状态机、重试、Checkpoint、Resume

参考的设计思想：
- **LangGraph** — State Graph、Checkpoint、Human-in-the-Loop
- **Magentic-One** — Task Ledger + Progress Ledger、Orchestrator + 嵌套循环、Stall 检测
- **AutoGen** — Agent-as-Tool、分层架构、描述驱动的路由
- **OpenHands** — 事件驱动的执行、Protocol-based 可扩展性

最终目标：将 GreenBook Assistant 从"LLM + Tool Calling"升级为真正的社区智能助手 Agent Runtime。
