# GreenBook 完整指南

> 面向零基础工程师。读完本文档即可理解整个 GreenBook 系统。

---

## 1. GreenBook 是什么？

GreenBook 是一个 **AI 驱动的社区知识平台**。它的核心是一个 **Goal-Driven Agent Runtime**——用户告诉 Agent **想做什么**（目标），Agent 理解目标、分解步骤、调用工具、观察结果、动态调整策略，直到目标完成。

**不是 ChatBot。** ChatBot 的模式是 "接收消息 → 返回回复"。GreenBook 的模式是 "理解目标 → 推理 → 行动 → 观察 → 调整 → 完成"。

**不是 Workflow Engine。** Workflow Engine 的模式是 "预定义步骤 → 顺序执行"。GreenBook 的模式是 "LLM 推理每一步该做什么"，没有固定的步骤模板。

---

## 2. 为什么这么设计？

### 核心原则

1. **LLM 做所有语义理解** — 意图识别、目标分解、工具选择、策略调整全部由 LLM structured output 完成
2. **Python 只做结构** — Pydantic schema 校验、确定性状态机、策略执行
3. **零硬编码关键词** — 没有 `if "发布" in text`、没有 `if intent == "create"`
4. **Fail-closed** — 不确定时拒绝，不做假设

### 为什么需要 Agent？

用户的请求不是简单的 API 调用：
- "帮我写一篇Java文章，明天发布" → 需要搜索、创作、定时发布，3个操作有依赖关系
- "修改刚才那篇的标题" → 需要理解"刚才那篇"是指哪个草稿
- "搜不到？换个角度再搜" → 需要根据 Observation 调整策略

Agent 需要：理解上下文 → 分解目标 → 选择工具 → 执行 → 观察结果 → 决定下一步。

---

## 3. 四个核心服务是什么？

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Agent API   │    │Agent Worker  │    │Creator Service│    │Java Backend  │
│   (FastAPI)  │    │   (Async)    │    │  (FastAPI+LG) │    │(Spring Boot) │
│   Port 8094  │    │              │    │   Port 8092   │    │  Port 8080   │
├──────────────┤    ├──────────────┤    ├──────────────┤    ├──────────────┤
│ HTTP 入口     │    │ 队列消费者    │    │ 内容创作管线   │    │ 社区数据权威   │
│ 对话管理      │    │ 重试后台      │    │ 研究→写作→评估 │    │ 帖子/用户/评论│
│ Agent 编排    │    │ 可靠执行      │    │ 人机协作审批   │    │ 定时发布状态机│
│ 审批处理      │    │ 崩溃恢复      │    │ Agentic RAG  │    │ 幂等写入     │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

### Agent API

用户请求的第一跳。接收 HTTP 请求，构建上下文，调用 Agent Core 的推理循环，返回结果。

### Agent Worker

后台执行进程。从 PostgreSQL 队列消费执行任务，驱动 ExecutionWorker 完成可靠执行（包含重试、检查点、崩溃恢复）。

### Creator Service

独立的 AI 内容创作服务。包含 7 个 Specialist Agent（研究、策略、写作、批评、评估），通过 LangGraph 状态图编排。支持人机协作审批和多轮修改。

### Java Backend

社区业务数据的**唯一权威**。Agent 不直接操作数据库，所有社区资源的读写必须通过 Java Backend 的 Agent Facade API。

---

## 4. Agent 如何理解用户？

```
用户消息: "帮我写一篇Java文章，明天发布"
  ↓
ContextBuilder.build() → ContextSnapshot
  ├─ 对话历史 (最近12条消息)
  ├─ 活跃 Task 列表
  ├─ 执行记录
  ├─ 用户偏好 + 记忆
  └─ 候选目标
  ↓
CommandInterpreter.interpret(message, context, llm)
  ↓ (LLM structured output, 零关键词)
Command {
  command: "CREATE",
  goals: [
    {text: "Write Java article", capabilities: ["SEARCH", "GENERATE"]},
    {text: "Schedule for tomorrow", capabilities: ["SCHEDULE"]}
  ]
}
```

**关键**: Command 不是通过关键词匹配生成的。LLM 理解完整的语义上下文（对话历史、活跃任务、用户偏好）后输出结构化 Command。

---

## 5. Agent 如何拆任务？

```
Command
  ↓
GoalDecomposer.decompose(command, context, capabilities, llm)
  ↓ (LLM 分解)
GoalTree:
  publish_article
  ├── research_topic     (capability: SEARCH)
  ├── create_article     (capability: GENERATE)
  └── schedule_publish   (capability: SCHEDULE)
```

每个 Goal 节点：
- 有明确的 `required_capabilities`
- 有依赖关系 (`dependencies`)
- 有预期产出 (`expected_output`)

GoalTree 验证：
1. 每个 capability 在目录中存在
2. Command.required_capabilities 都被满足
3. 无环 (DAG)

---

## 6. Agent 如何执行？

### AgentLoop: Observe → Reason → Act → Reflect

```
┌──────────────────────────────────────────────────────────┐
│                    AgentLoop.run()                        │
│                                                           │
│  Observe: 收集当前状态                                    │
│    - Goal 进展, Task 状态, 上一步 Tool 结果, 相关记忆      │
│                                                           │
│  Reason: LLM 决策 next action                             │
│    - TOOL_CALL: 调用工具                                  │
│    - CREATE_TASK: 创建子任务 (带 Plan)                    │
│    - ASK_USER: 需要人类输入                               │
│    - FINISH: 目标完成                                     │
│                                                           │
│  Act: 执行 action                                         │
│    - ToolPolicyGate 检查 → 审批/拒绝/队列/同步             │
│    - ToolRuntime.invoke → MCP → Java/Creator              │
│                                                           │
│  Reflect: 评估进展                                         │
│    - 目标完成? → FINISH                                   │
│    - 需要调整? → DynamicPlanner.replan                    │
│    - 需要继续? → 回到 Observe                             │
│                                                           │
│  终止条件: FINISH | WAITING_HUMAN | FAILED | BUDGET_EXCEEDED│
└──────────────────────────────────────────────────────────┘
```

### Reliable Execution

```
Agent Action → ExecutionInput (typed plan)
  ↓
ExecutionStateManager.init_execution → PENDING
  ↓
ExecutionQueue.enqueue → READY
  ↓
ExecutionQueueWorker.claim → CLAIMED
  ↓
ExecutionWorker.run()
  ├─ start_execution → RUNNING
  ├─ for each step:
  │   ├─ PENDING → RUNNING
  │   ├─ CapabilityExecutor → ToolRuntime → MCP → Java/Creator
  │   ├─ RUNNING → COMPLETED (or FAILED/WAITING_APPROVAL)
  │   └─ Save checkpoint
  └─ COMPLETED → artifacts + projection
```

**安全保证**:
- **幂等**: Tool 调用有 Ledger 保护，COMPLETED → 直接重放
- **重试**: FAILED_RETRYABLE → RetryBackgroundWorker → retry → requeue
- **崩溃恢复**: Checkpoint replay + CLAIMED 租约过期回收
- **双消费保护**: Execution Lease (SELECT FOR UPDATE) + Queue claim rowcount

---

## 7. Tool 如何调用？

```
Agent Decision → ToolSelector (LLM 选择)
  ↓
"content.create_draft"

ToolPolicyGate.enforce("content.create_draft")
  ↓ policy: IDEMPOTENT_WRITE, timeout=240s
  → execution_mode = QUEUE

ToolRuntime.invoke(ctx)
  ├─ 幂等检查: Ledger 中已 COMPLETED? → replay
  ├─ 未执行过: record_start → execute
  └─ handler() → ToolResult

GreenBookMCPServer.execute_tool("content.create_draft", ...)
  ├─ Pydantic schema 校验 args
  ├─ handler(ctx, **args) → ToolResult
  └─ Pydantic schema 校验 result

content.create_draft(ctx, title, instruction, ...):
  ├─ CreatorClient.create_task(...)     # HTTP → Creator Service
  ├─ CreatorClient.wait_for_completion  # Poll
  ├─ CreatorClient.get_artifact(...)    # HTTP
  └─ JavaClient.create_draft(...)       # HTTP → Java Backend
```

---

## 8. MCP 作用是什么？

MCP (Model Context Protocol) 在 GreenBook 中是一个 **in-process tool runtime**，不是独立部署的 MCP 服务器。

它的作用：
1. **统一执行边界** — 所有 Tool 调用经过同一个入口、同一套 schema 校验、同一个结果信封
2. **上下文注入** — ToolContext (identity, session, trace) 由 runtime 注入，从不从 LLM 参数获取
3. **安全 handshake** — 编排多步写操作 (Creator 生成 → Java 持久化)，包含 write-then-verify
4. **失败分类** — 所有下游错误统一映射到 ToolResult 信封，Worker 基于一致语义决策

---

## 9. Java 如何提供业务能力？

Java Backend 是社区业务数据的**唯一权威**。Agent 通过 Agent Facade API 访问：

| 能力 | API | 说明 |
|------|-----|------|
| 搜索帖子 | `GET /agent/posts/search` | MySQL LIKE |
| 帖子详情 | `GET /agent/posts/{id}` | 含 body (max 512KB) |
| 草稿 CRUD | `POST/PUT/GET /agent/drafts` | 乐观锁 expectedVersion |
| 定时发布 | `POST/PUT/DELETE /agent/publications/schedules` | 状态机 |
| 立即发布 | `POST /agent/publications/publish-now` | 需要审批 |
| 评论互动 | `GET/POST /agent/comments` | 游标分页 |
| 分析 | `GET /agent/analytics` | 聚合计数 |

**Agent 不能直接访问数据库** — 所有权在 Java，包括乐观锁、幂等写入、发布状态机、事件驱动更新。

---

## 10. Creator 如何协作？

Main Agent 通过 MCP content handler 调用 Creator：

```
Main Agent → "content.create_draft"
  → MCP handler
    → CreatorClient.create_task(kind=CREATE_CONTENT)
    → CreatorService:
        MemoryAgent → ContentAnalyzerAgent → ResearchAgent
        → StrategyAgent → Topic Options
        → (Human Topic Selection)
        → Outline → (Human Outline Approval)
        → WriterAgent → CriticAgent → WriterAgent (修改循环)
        → EvaluationAgent → FINAL_CONTENT artifact
    → CreatorClient.wait_for_completion
    → CreatorClient.get_artifact
    → JavaClient.create_draft → 保存到 Java Backend
```

**为什么独立?** Creator 是长时间运行的任务（研究+创作+多轮修改），需要独立的持久化控制面、Human-in-the-Loop、评估框架和水平扩缩容。

---

## 11. 一个完整请求如何流转？

以 **"帮我写一篇Java文章，明天发布"** 为例：

```
Step 1: Frontend
  POST /api/v1/agent/conversations/{id}/messages
  Body: {content: "帮我写一篇Java文章，明天发布"}

Step 2: Agent API (JWT 验证)
  JwtAuthMiddleware → 验证 Bearer Token → AuthContext

Step 3: Context Building
  ContextBuilder.build(conversation_id)
    → 对话历史 + Task 列表 + 执行记录 + 偏好 + 记忆

Step 4: Command Understanding (LLM)
  CommandInterpreter → Command {command: "CREATE", goals: [...]}

Step 5: Goal Decomposition (LLM)
  GoalDecomposer → GoalTree:
    publish_java_article
    ├── research_java_topic
    ├── create_java_article
    └── schedule_publish

Step 6: Task Persistence
  TaskManager.create_task → Task(CREATED)
  TaskManager.bind_goal_tree → Task(READY)

Step 7: AgentLoop
  Observe: goal=create_article, context=...
  Reason (LLM): "先搜索Java社区内容" → TOOL_CALL: community.search_public_posts
  Act → ToolRuntime → MCP → JavaClient.search_posts → 搜索结果
  Reflect: "搜索完成, 资料够用" → 继续

  Reason (LLM): "调用Creator生成文章" → CREATE_TASK
  Act → GoalCompiler.compile → ExecutionSubmission
    → Queue Message → Worker → Creator + Java
  Reflect: "草稿已保存" → 继续

  Reason (LLM): "设置明天发布" → TOOL_CALL: publication.schedule
  Act → ToolRuntime → MCP → JavaClient.create_schedule
  Reflect: "定时发布已设置" → 继续

  Reason (LLM): "所有子目标完成" → FINISH

Step 8: AgentLoop Result
  AgentRunResult {
    finished: true,
    artifacts: [draft_ref, schedule_ref],
    execution_ids: [exec_1, exec_2]
  }

Step 9: RuntimeAgentService.submit_plan
  创建 Execution → enqueue → Worker 消费 → ExecutionWorker.run()
  → ArtifactStore → CompletionProjection

Step 10: Response
  202 RunAcceptedResponse {
    run_id: "abc123",
    execution_id: "exec_xyz",
    events_url: "/api/v1/executions/exec_xyz/events"
  }

Step 11: Frontend Poll/SSE
  GET /api/v1/executions/exec_xyz/events
  → [STEP_STARTED, TOOL_INVOKED, ARTIFACT_CREATED, EXECUTION_COMPLETED]

Step 12: User Sees Result
  "草稿已保存: Java文章
   定时发布: 2026年8月13日 22:00"
```

---

## 快速导航

| 想了解... | 阅读 |
|-----------|------|
| Java Backend 如何暴露 Agent API | [BACKEND_ANALYSIS.md](BACKEND_ANALYSIS.md) |
| Agent API 如何处理请求 | [AGENT_API_ANALYSIS.md](AGENT_API_ANALYSIS.md) |
| Worker 如何可靠执行 | [WORKER_ANALYSIS.md](WORKER_ANALYSIS.md) |
| Agent Core 各模块职责 | [AGENT_CORE_ANALYSIS.md](AGENT_CORE_ANALYSIS.md) |
| Tool 调用完整链路 | [TOOL_RUNTIME_ANALYSIS.md](TOOL_RUNTIME_ANALYSIS.md) |
| MCP 层设计 | [MCP_ANALYSIS.md](MCP_ANALYSIS.md) |
| Creator 创作管线 | [CREATOR_ANALYSIS.md](CREATOR_ANALYSIS.md) |
| 总体架构 | [GREENBOOK_SYSTEM_OVERVIEW.md](GREENBOOK_SYSTEM_OVERVIEW.md) |
| 目录结构 | [CURRENT_TREE.md](CURRENT_TREE.md) |
| 模块通信 | [COMMUNICATION.md](COMMUNICATION.md) |
| 技术债 | [TECH_DEBT.md](TECH_DEBT.md) |

---

## 关键术语

| 术语 | 定义 |
|------|------|
| **Command** | 用户目标的 LLM 结构化表示 (CREATE/MODIFY/CANCEL/QUERY) |
| **GoalTree** | 目标分解树 (父目标 → 子目标, DAG) |
| **AgentLoop** | Observe→Reason→Act→Reflect 循环 |
| **Execution** | 一次 Plan 的可靠执行 (Queue → Worker → Steps) |
| **ToolRuntime** | Tool 调用运行时 (幂等检查+执行+记账) |
| **MCP** | MCP-compatible in-process tool runtime |
| **Ledger** | 幂等执行账本 (COMPLETED 可重放) |
| **Checkpoint** | 执行检查点 (已完成步骤+结果快照) |
| **Lease** | 执行租约 (防跨进程并发) |
| **Artifact** | 不可变产物 (CAPABILITY_STEP_OUTPUT) |
| **Projection** | 执行完成的持久化读模型 |
| **SessionContext** | 用户会话的活跃资源 (active_draft_id等) |
| **ContextSnapshot** | 单次请求的上下文快照 (对话+Task+记忆) |
