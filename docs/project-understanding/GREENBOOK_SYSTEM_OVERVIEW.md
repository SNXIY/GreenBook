# GreenBook 系统总览

## 1. 系统定位

**GreenBook 是一个 AI 驱动的社区知识平台。** 

它不是一个 ChatBot。一个 ChatBot 接收消息、返回回复。GreenBook 的 Agent 系统中，LLM 是一个推理引擎——理解用户的**目标**，分解为可执行的**步骤**，通过**工具**操作社区资源（帖子、评论、定时发布），并通过**可靠执行层**确保每一步的正确性和可恢复性。

### 解决什么问题？

1. **降低创作者门槛**: AI 辅助完成从研究到发布的完整创作流程
2. **自动化社区运营**: 搜索、分析、定时发布、评论互动由 Agent 驱动
3. **人机协作**: Agent 执行操作，人类审批关键决策
4. **可靠异步执行**: 长时间运行的任务（创作、发布）不会因进程崩溃而丢失

### 为什么需要 Agent？

简单的 "keyword → API call" 映射无法处理：
- 复杂多步目标 ("分析AI趋势，写文章，明天发布")
- 上下文引用 ("修改刚才那篇")
- 策略调整 ("搜不到？换个角度再搜")
- 人机协作审批 ("内容准备好了，是否发布？")

Agent 需要理解目标、分解步骤、调用工具、观察结果、调整策略——这是一个推理循环，不是 if-else 路由。

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        zhiguang-fe                              │
│                    (React/TypeScript/Vite)                      │
│                        Frontend :5173                          │
└─────────────────────────────┬───────────────────────────────────┘
                              │ HTTP/SSE
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      apps/agent_api                             │
│                    (FastAPI/uvicorn)                            │
│                      Agent API :8094                            │
│                                                                 │
│  POST /messages → ConversationRuntimeAdapter                   │
│      ├─ ContextBuilder → ContextSnapshot                       │
│      ├─ CommandInterpreter (LLM) → Command                     │
│      ├─ GoalDecomposer (LLM) → GoalTree                        │
│      ├─ AgentLoop (Observe→Reason→Act→Reflect)                 │
│      └─ RuntimeAgentService (submit plan)                      │
│                                                                 │
│  GET /executions/{id}/stream → SSE events                      │
│  POST /executions/{id}/approve → Human-in-Loop                 │
└─────────────────────────────┬───────────────────────────────────┘
                              │ Queue / Direct
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     apps/agent_worker                           │
│                   (Async Worker Process)                        │
│                                                                 │
│  ExecutionQueueWorker                                           │
│    claim → ExecutionWorker.run() → ack                         │
│  RetryBackgroundWorker                                          │
│    claim → RetryManager → requeue → complete                    │
└─────────────────────────────┬───────────────────────────────────┘
                              │ Tool Calls
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                  packages/agent_core                            │
│                    (Core Library)                               │
│                                                                 │
│  Intelligence:                                                  │
│    command/  goal/  agent/  planning/  context/  memory/       │
│                                                                 │
│  Execution:                                                     │
│    execution/  artifact/  human/  observability/               │
└─────────────────────────────┬───────────────────────────────────┘
                              │ MCP / Tool Contracts
                              ▼
┌──────────────────────────────┬──────────────────────────────────┐
│  services/greenbook_mcp     │       creator-agent               │
│  (MCP Tool Runtime)         │       (Creator Service) :8092     │
│                              │                                  │
│  16 tool handlers            │  7 specialist agents              │
│  → Java Backend              │  LangGraph supervisor loop        │
│  → Creator Service           │  Research→Strategy→Write→Critique│
└────────────────┬─────────────┴─────────────┬────────────────────┘
                 │                           │
                 ▼                           ▼
┌────────────────────────┐    ┌──────────────────────────┐
│   apps/backend          │    │   packages/creator_client │
│   (Java Spring Boot)    │    │   + creator-agent API     │
│   Java Backend :8080    │    │                          │
│                          │    │                          │
│   Agent Facade API       │    │   Task/Run/SSE API        │
│   帖子/草稿/发布/评论    │    │                          │
│   MySQL + Redis + Kafka  │    │   PostgreSQL + Redis       │
│                          │    │   + Qdrant                 │
└──────────────────────────┘    └──────────────────────────┘
```

---

## 3. 核心服务职责

| 服务 | 端口 | 负载 | 职责 |
|------|------|------|------|
| **zhiguang-fe** | 5173 | React/Vite | 用户界面 |
| **agent_api** | 8094 | FastAPI | HTTP 入口, 对话管理, Agent 编排 |
| **agent_worker** | — | Async Python | 队列消费, 重试, 可靠执行 |
| **backend** | 8080 | Java Spring Boot | 社区业务事实层 |
| **creator-agent** | 8092 | FastAPI + LangGraph | 内容创作服务 |
| **PostgreSQL** | 25432 | — | Agent Runtime + Creator 持久化 |
| **MySQL** | 33306 | — | 社区业务数据 |
| **Redis** | 26379 | — | 缓存/限流/计数器 |
| **Kafka** | 39092 | Redpanda | 事件流 |
| **Qdrant** | 26333/4 | — | 语义搜索 |

---

## 4. 模块职责表

| 模块 | 路径 | 职责 | 是否核心 | 是否可修改 |
|------|------|------|----------|------------|
| agent_core | packages/agent_core | Agent 推理+执行核心 | ✓ 核心 | 是 |
| contracts | packages/contracts | 共享契约 (ToolResult, ToolPolicy, AuthContext) | ✓ 核心 | 谨慎 (影响所有下游) |
| security | packages/security | JWT 验证 + 安全策略 | ✓ 核心 | 谨慎 |
| java_client | packages/java_client | Java Backend HTTP 客户端 | ✓ 核心 | 是 |
| creator_client | packages/creator_client | Creator Service HTTP 客户端 | ✓ 核心 | 是 |
| greenbook_mcp | services/greenbook_mcp | MCP Tool Runtime (16 handlers) | ✓ 核心 | 是 |
| agent_api | apps/agent_api | HTTP 入口, 路由, 适配 | ✓ 核心 | 是 |
| agent_worker | apps/agent_worker | Worker 进程组装 | ✓ 核心 | 是 |
| evaluation | packages/evaluation | 行为评估框架 (12 golden cases) | 辅助 | 是 |
| observability | packages/observability | 空壳 (未实现) | 辅助 | 是 |
| backend | apps/backend | Java 社区后端 | ✓ 核心 | 谨慎 (Java 团队) |
| creator-agent | creator-agent | Creator 创作服务 | ✓ 核心 | 谨慎 (独立服务) |
| zhiguang-fe | zhiguang-fe | 前端 React | ✓ 核心 | 是 |
| zhiguang-be | zhiguang-be | 旧数据库 DDL (历史) | 历史遗留 | 否 |

---

## 5. 一次完整任务执行

以 **"帮我写一篇Java文章，明天发布"** 为例：

```
1. 用户输入
   Frontend → POST /api/v1/agent/conversations/{id}/messages
   Body: {content: "帮我写一篇Java文章，明天发布"}

2. API 接收
   JwtAuthMiddleware: 验证 Bearer Token → AuthContext
   routes.py: 生成 run_id + trace_id, 准备对话历史

3. 上下文构建
   ContextBuilder.build(conversation_id, user_id)
     → 对话历史 (最近 12 条)
     → 活跃 Task 列表
     → 最近执行记录
     → 用户偏好 + 记忆

4. 命令理解
   CommandInterpreter.interpret(user_message, context, llm)
     → LLM structured output:
     {
       "command": "CREATE",
       "goals": [
         {"description": "Write a Java article", "required_capabilities": ["SEARCH", "GENERATE"]},
         {"description": "Schedule publish for tomorrow", "required_capabilities": ["SCHEDULE"]}
       ]
     }

5. 目标分解
   GoalDecomposer.decompose(command, context, capabilities, llm)
     → GoalTree:
       publish_java_article
       ├── research_java_topic (SEARCH)
       ├── create_java_article (GENERATE)
       └── schedule_publish (SCHEDULE)

6. Task 管理
   TaskManager.create_task(command) → Task(CREATED)
   TaskManager.bind_goal_tree(task, goal_tree) → Task(READY)

7. AgentLoop 推理
   AgentLoop.run(command, goal_tree, ...)
     ├─ Reason: "需要先搜索Java社区内容" → TOOL_CALL
     ├─ Act: ToolRuntime.invoke → MCP → JavaClient.search_posts
     ├─ Reflect: "搜索完成, 资料收集完毕, 下一步生成文章"
     ├─ Reason: "调用Creator生成文章" → CREATE_TASK
     ├─ Act: GoalCompiler.compile + ExecutionSubmission
     │   → CreatorClient.create_task → Creator 生成内容
     │   → JavaClient.create_draft → 保存草稿
     ├─ Reflect: "草稿已保存, 开始设置定时发布"
     ├─ Reason: "设置明天发布" → TOOL_CALL
     └─ Act: ToolRuntime.invoke → MCP → JavaClient.create_schedule

8. 执行管线 (TaskPlan → Execution)
   RuntimeAgentService.submit_plan(plan)
     → ExecutionStateManager.init_execution (PENDING)
     → execution_queue.enqueue (READY)
     → (Queue Worker 消费)
     → ExecutionWorker.run()
       ├─ Step 1: SEARCH → ToolRuntime → MCP → Java
       ├─ Step 2: GENERATE → ToolRuntime → MCP → Creator + Java
       └─ Step 3: SCHEDULE → ToolRuntime → MCP → Java

9. 完成投影
   ExecutionWorker → COMPLETED
     → ArtifactStore.create (draft + schedule artifacts)
     → ExecutionResultProjection (持久化)
     → TaskProvider.persist_completion_projection
     → Append Assistant Message

10. 响应返回
    202 RunAcceptedResponse {
      run_id: "abc123",
      execution_id: "exec_xyz",
      events_url: "/api/v1/executions/exec_xyz/events"
    }
    前端通过 events_url 轮询/SSE 获取执行进度
```

---

## 6. 关键设计决策

1. **LLM 做语义, Python 做结构**: 所有理解/决策由 LLM structured output 完成。Python 代码只做 schema 校验、状态机、策略执行。

2. **零关键词路由**: 没有中文关键词分类、没有 if-else 意图链。Command 由 LLM 根据完整上下文理解。

3. **双层架构**: Intelligence Layer (Agent Loop, Planning, Reasoning) 和 Reliable Execution Layer (Queue, Worker, Retry, Checkpoint) 分离。

4. **Queue/Direct 双模式**: 所有执行可以同步(direct)或异步(queue)分发。

5. **幂等性为先**: 所有关键操作有确定性 idempotency key, Tool invocation 通过 Ledger 保护。

6. **Human-in-the-Loop**: DESTRUCTIVE_WRITE 操作强制审批, 审批信息持久化(跨进程重启).

7. **按业务拆服务**: Creator 独立部署(独立生命周期,独立扩缩容,独立评估). Java Backend 独立部署(社区数据所有权).
