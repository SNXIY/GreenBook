# GreenBook Agent API Analysis

## 1. 定位

Agent API (`apps/agent_api`) 是整个 Agent 系统的**HTTP 入口**。它是用户请求进入系统后的"第一跳"。

---

## 2. 项目结构

```
apps/agent_api/
├── pyproject.toml
└── greenbook_agent_api/
    ├── main.py                              # FastAPI 入口 + 生命周期
    ├── api/
    │   ├── routes.py                        # /api/v1/agent/* (对话、消息)
    │   ├── runtime_routes.py                # /api/v1/* (执行状态、事件、控制)
    │   └── tool_helpers.py                  # Tool schema 工具函数
    ├── models/
    │   ├── runtime_context.py               # RuntimeContext, TaskContext, TargetContext
    │   └── runtime_result.py                # RuntimeResult
    └── services/
        ├── conversation_runtime_adapter.py  # ★ 消息 → Agent Core 核心适配器
        ├── runtime_agent_service.py         # ★ Runtime 执行管线
        ├── task_provider.py                # Task 持久化
        ├── approval_runtime_service.py      # 审批持久化
        ├── completion_projection_coordinator.py  # 完成投影
        ├── execution_completion_publisher.py     # 完成回调
        ├── execution_authorizer.py          # 执行授权
        ├── execution_presenter.py           # 结果展示
        ├── execution_credential_broker.py   # 进程内凭证传递
        ├── queue_execution_handler.py       # 队列消息处理
        ├── conversation_control_service.py  # 暂停/恢复/重试
        ├── result_resolver.py               # 结果解析
        └── runtime_linking.py               # run↔execution 绑定
```

---

## 3. FastAPI 入口 (main.py)

### 生命周期

```
startup:
  1. 加载 .env 配置
  2. 构建 JavaClient + CreatorClient (HTTP 客户端)
  3. 构建 RuntimeContainer (持久化配置)
  4. 构建 GreenBookMCPServer (Tool 运行时)
  5. 构建 LLM Client (AsyncOpenAI, DeepSeek)
  6. 构建 MemoryManager, ContextBuilder
  7. 构建 TaskProvider, TaskManager
  8. 构建 RuntimeAgentService (执行引擎)
  9. 构建 ConversationRuntimeAdapter (核心适配器)
  10. 构建 ApprovalRuntimeService, ExecutionAuthorizer
  11. 构建 ExecutionCompletionPublisher
  12. 可选: 启动进程内 Queue Worker + Retry Worker
  13. 启动 reconcile: 恢复最近 100 个队列消息的投影

shutdown:
  1. 停止后台 Worker
  2. 关闭 DB 连接池
  3. 关闭 HTTP 客户端
```

### 中间件

```
请求 → JwtAuthMiddleware (验证 Bearer token → AuthContext)
     → CORSMiddleware
     → 路由处理
```

AuthContext 存储在 `request.state.auth_context`，同时注册到 ExecutionCredentialBroker（供进程内 Queue Worker 恢复凭证）。

---

## 4. 路由

### 对话路由 (`/api/v1/agent/*`)

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/conversations` | 对话列表 |
| POST | `/conversations` | 创建对话 |
| GET | `/conversations/{id}/tasks` | 任务索引 |
| POST | `/conversations/{id}/messages` | **发送消息** (主入口) |
| GET | `/conversations/{id}/messages` | 消息历史 |
| GET | `/runs/{run_id}` | 运行状态 |
| GET | `/runs` | 运行列表 |
| POST | `/approvals/{id}/approve\|reject` | 审批决定 |

### Runtime 路由 (`/api/v1/*`)

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/executions` | 执行列表 (游标分页) |
| GET | `/executions/{id}` | 执行状态 |
| GET | `/executions/{id}/steps` | 步骤快照 |
| GET | `/executions/{id}/events` | 历史事件 |
| GET | `/executions/{id}/timeline` | 时间线 |
| GET | `/executions/{id}/stream` | SSE 事件流 |
| POST | `/executions/{id}/pause` | 暂停 |
| POST | `/executions/{id}/resume` | 恢复 |
| POST | `/executions/{id}/cancel` | 取消 |
| POST | `/executions/{id}/steps/{step_id}/retry` | 重试步骤 |

---

## 5. 请求生命周期

```
用户发送消息 "帮我写一篇Java文章，明天发布"

POST /api/v1/agent/conversations/{id}/messages
  │
  ├─ JwtAuthMiddleware: 验证 JWT → AuthContext
  │
  ├─ routes.py send_message / _send_runtime_message:
  │   1. 生成 run_id + trace_id
  │   2. 加载对话历史
  │   3. 调用核心适配器
  │
  ├─ ConversationRuntimeAdapter.execute():
  │   1. ContextBuilder.build() → ContextSnapshot
  │      (对话历史 + 任务状态 + 执行记录 + 产物 + 偏好 + 记忆)
  │   2. CommandInterpreter.interpret(message, context) → Command
  │      (LLM structured output, 零关键词)
  │   3. GoalDecomposer.decompose(command, context) → GoalTree
  │      (LLM 分解复杂目标)
  │   4. TaskManager.bind_goal_tree(command, goal_tree)
  │      (持久化 Task)
  │   5. AgentLoop.run(command, goal_tree, ...)
  │      ├─ Observe: 收集状态
  │      ├─ Reason: LLM 决策 next action
  │      ├─ Act: 调用 Tool / 提交执行
  │      └─ Reflect: 评估进展
  │   6. 返回 RuntimeResult
  │
  ├─ 投影到 run_store (兼容层)
  ├─ 持久化审批 (如有)
  ├─ 保存 Session
  ├─ 发布完成投影 (terminal 状态)
  │
  └─ 返回 202 RunAcceptedResponse {
       run_id, status, execution_id, events_url
     }
```

---

## 6. RuntimeResult → 响应

```
RuntimeResult
  │
  ├─ (direct 模式) → 同步返回完整结果
  ├─ (queue 模式) → 202 QUEUED (稍后通过 events_url 轮询)
  ├─ (detached) → 202 RUNNING (后台执行)
  └─ (WAITING_HUMAN) → 返回审批信息
```

---

## 7. 关键服务

### ConversationRuntimeAdapter (核心适配器)

```
职责: 用户消息 → Agent Core 的桥梁
输入: user_message, conversation_context
输出: RuntimeResult
调用: CommandInterpreter → GoalDecomposer → AgentLoop → RuntimeAgentService
被: routes.py POST /messages 调用
```

### RuntimeAgentService (执行管线)

```
职责: 执行单个 Agent Turn
输入: RuntimeContext (含 TaskContext + ExecutionInput)
输出: RuntimeResult
调用: MemoryManager → CapabilityMapper → PlanValidator
      → ExecutionWorker → ToolRuntime → MCP
被: ConversationRuntimeAdapter 和 QueueExecutionHandler 调用
```

### CompletionProjectionCoordinator (完成投影)

```
职责: 执行完成后持久化所有读模型
输入: RuntimeResult + ExecutionQueueMessage
输出: 无
调用: ResultResolver → ExecutionProjectionAdapter
      → TaskProvider.persist_completion_projection
      → 更新 Conversation Session
      → 合并 Assistant Message
被: ExecutionCompletionPublisher 调用 (同步路径和队列路径)
```

---

## 8. 三种分发模式

| 模式 | 配置 | 行为 |
|------|------|------|
| direct | `GREENBOOK_AGENT_EXECUTION_DISPATCH=direct` | API 进程内同步执行完整 AgentLoop |
| queue | `GREENBOOK_AGENT_EXECUTION_DISPATCH=queue` | API 将执行入队，Worker 异步消费 |
| in-process-worker | queue + `GREENBOOK_AGENT_IN_PROCESS_WORKER=true` | API 进程内同时运行 Queue Worker |

---

## 9. 审批处理

```
RuntimeResult(status=WAITING_HUMAN)
  │
  ├─ ApprovalRuntimeService.capture_result()
  │   持久化 ApprovalRequest
  │   设置 session.pending_approval
  │
  ├─ 用户 POST approve/reject
  │   ├─ APPROVE: state.approve_and_resume()
  │   │   → queue: requeue 执行
  │   │   → direct: resume_human_interaction(ACCEPT)
  │   └─ REJECT: state.cancel_execution()
  │
  └─ 返回结果
```

---

## 10. 与 Agent Core 的边界

Agent API **从不直接调用 Agent Core 的函数**。所有调用通过以下三个边界服务：

1. **ConversationRuntimeAdapter** — 命令理解、目标分解、AgentLoop 编排
2. **RuntimeAgentService** — Plan 验证、Worker、Tool Runtime、状态管理
3. **TaskProvider** — Task 持久化

这三个服务在 `main.py` 的 `lifespan` 中组装，存储在 `app.state` 上，路由通过 `request.app.state` 访问。
