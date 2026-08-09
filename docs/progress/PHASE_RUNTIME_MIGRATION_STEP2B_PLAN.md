# Phase 2-B：Conversation Message → RuntimeAgentService 迁移设计

## 0. 审计范围与结论

- 分支：feature/runtime-http-migration
- 基线：b9714dc feat(runtime): register execution http routes
- 本阶段只读设计，不修改 Python、路由、Runtime 核心、数据库或 migration。
- 当前 message 入口仍走 CommunityOperationsAssistant。
- Runtime HTTP API 已注册，但尚未接管聊天 message。
- 需要一个 API 边界的 ConversationRuntimeAdapter。
- Runtime 创建 PlanExecution 后不得自动回退 Legacy，避免重复副作用。

目标链路：

~~~text
POST /api/v1/assistant/conversations/{conversation_id}/messages
  -> ConversationRuntimeAdapter
  -> TaskUnderstanding / IntentSpec
  -> TaskContext / RuntimeContext
  -> RuntimeAgentService
  -> TaskPlan / PlanExecution
  -> ExecutionWorker / ToolRuntime
  -> RuntimeResult
  -> AssistantResponse / compatibility RunAcceptedResponse
~~~

PlanExecution、ExecutionStateManager、ExecutionRepository、ExecutionEventStore 仍是唯一 Runtime 执行状态链；assistant_runs 和旧 run_store 只能作为兼容投影或回滚路径。

## 1. 当前 message endpoint 与调用链

### 1.1 HTTP 入口

文件：apps/assistant_api/greenbook_assistant_api/api/routes.py

路由前缀：/api/v1/assistant

函数：send_message()

接口：

~~~text
POST /api/v1/assistant/conversations/{conversation_id}/messages
~~~

请求模型：

~~~python
class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
~~~

身份与会话前置条件：

- JWT middleware 将 token 解析到 request.state.auth_context。
- _get_session() 只接受已经存在且归属当前 user_id/tenant_id 的 conversation。
- timezone 来自请求体，默认沿用 Asia/Shanghai。
- 旧会话保存在 app.state.conversation_store。

### 1.2 实际旧调用链

~~~text
POST message
  -> routes.send_message
  -> _get_auth / _get_session
  -> 从 app.state.message_store 读取公开 user/assistant history
  -> 写入 user message
  -> 创建 trace_id、run_id
  -> CommunityOperationsAssistant(...)
  -> CommunityOperationsAssistant.run(...)
  -> route 内部 tool_handler
  -> app.state.mcp.execute_tool(...)
  -> 收集旧 {event, data} 事件
  -> 写入 assistant message_store
  -> 写入 app.state.run_store
  -> 返回 RunAcceptedResponse（202）
~~~

当前 send_message() 没有调用：

- app.state.runtime_agent_service
- TaskUnderstanding
- IntentCompiler
- RuntimeContext
- ExecutionStateManager
- ExecutionRepository
- ExecutionEventStore

所以当前请求不会自然产生 IntentSpec、TaskContext、TaskPlan、PlanExecution 或 execution_id。

### 1.3 当前旧输出与副作用

成功响应：

~~~python
RunAcceptedResponse(
    run_id=run_id,
    conversation_id=conversation_id,
    status=run_status,
    events_url=f"/api/v1/assistant/runs/{run_id}/events",
)
~~~

旧路径：

- 只返回 run_id，不返回 execution_id。
- assistant 文本写入 message_store。
- 事件写入 run record 的 events。
- status 是旧的 COMPLETED 或 WAITING_APPROVAL。
- GET /runs、GET /runs/{run_id} 和旧 SSE 继续读 run_store。
- 当前分支 routes.py 的 send_message() 没有直接操作 assistant_runs 表；架构文档中的数据库 projection 不能替代当前代码事实。

失败路径：

- 模型异常：run_store 写入 FAILED，返回 502。
- 工具失败：旧 code 映射为 HTTP 4xx/5xx。
- approval：写入 approval_store 和 SessionContext.pending_approval。
- 旧 HTTP 错误与 RuntimeResult 状态没有统一边界。

## 2. CommunityOperationsAssistant 输入/输出模型

文件：packages/assistant_core/greenbook_assistant_core/agent.py

构造输入：

~~~python
CommunityOperationsAssistant(
    llm=app.state.llm,
    model=app.state.model,
    tools_schema=_build_tool_schemas(),
    system_prompt=_SYSTEM_PROMPT,
)
~~~

run() 输入：

~~~python
await assistant.run(
    user_message=body.content,
    session=session,
    tool_handler=tool_handler,
    conversation_history=conversation_history,
    trace_id=trace_id,
    run_id=run_id,
    on_tool_start=on_tool_start,
    on_tool_complete=on_tool_complete,
    on_assistant_delta=on_assistant_delta,
)
~~~

旧模型的关键语义：

- user_message 是一条原始文本。
- session 是可变 SessionContext，包含 active_draft_id、active_schedule_id、pending_approval 和 recent_entities。
- tool_handler 在 API route 内部负责目标绑定、相对时间、approval 和 MCP 调用。
- conversation_history 只包含公开 user/assistant 消息。
- run_id 是旧 turn ID。

run() 返回普通 dict：

~~~python
{
    "run_id": str,
    "trace_id": str,
    "content": str,
    "tool_rounds": int,
    "session_snapshot": {
        "conversation_id": str,
        "active_draft_id": str | None,
        "active_post_id": str | None,
        "active_schedule_id": str | None,
    },
}
~~~

它不返回：

- IntentSpec
- TaskContext
- TaskPlan
- PlanExecution
- execution_id
- canonical step/event 状态
- Runtime artifact 或 retry metadata

旧 Agent 是隐式 tool-calling loop，不是 Planner/ExecutionWorker 链。

## 3. RuntimeAgentService 输入/输出模型

文件：apps/assistant_api/greenbook_assistant_api/services/runtime_agent_service.py

构造依赖：

~~~python
RuntimeAgentService(
    *,
    repository=app.state.execution_repository,
    event_store=app.state.execution_event_store,
)
~~~

execute() 入口：

~~~python
await runtime_agent_service.execute(
    ctx: RuntimeContext,
    detach: bool = False,
    completion_callback: RuntimeCompletionCallback | None = None,
)
~~~

RuntimeAgentService 不接受 HTTP Request 或单独的 message 字符串；它要求 RuntimeContext.task_context 已经具备：

- task_id
- task_intent
- goal
- target/constraints（如适用）

缺失时返回 TASK_CONTEXT_REQUIRED，不会自行理解文本或创建 TaskContext。

RuntimeContext 的关键字段：

| 字段 | 来源/作用 |
|---|---|
| conversation_id | 对话边界 |
| run_id | 兼容 turn ID，不等于 execution_id |
| trace_id | trace/audit |
| task_id | 已解析 Task |
| execution_id | 初始为空，由 Worker 回写 |
| task_context | 已解析 TaskContext，必填 |
| user_id/tenant_id | 身份隔离 |
| timezone | 时间解析 |
| user_message | 原始用户目标 |
| conversation_history | 公开 history |
| task_intent | TaskIntent 兼容投影 |
| session | SessionContext |
| active_draft_id/active_schedule_id | 目标绑定 |
| mcp/llm/model/auth | 执行依赖 |

内部链路：

~~~text
TaskContext
  -> PlanningContext
  -> TaskOrchestrator.generate_plan()
  -> TaskPlan
  -> PlanValidator
  -> ExecutionWorker.init_from_plan()
  -> PlanExecution.execution_id
  -> ExecutionStateManager / ToolRuntime
  -> RuntimeResult
~~~

RuntimeResult 关键字段：

- success
- status：RUNNING、COMPLETED、FAILED、WAITING_APPROVAL 等
- run_id、task_id、execution_id
- content、summary
- error_code、error_message、retryable
- steps、events
- artifacts、artifact_ids、schedule
- approval_id、approval_data、approval
- failure_state、presentation

detach=True 时，Runtime 立即返回 status=RUNNING 和 execution_id，后台继续执行，完成后通过 callback 返回最终 RuntimeResult。初始 RUNNING 不能标记 success，也不能生成“已完成”。

现有 presentation 组件：

- services/execution_projection_adapter.py
- services/execution_presenter.py
- services/assistant_service.py

ExecutionResultPresenter 已能生成 AssistantResponse，但当前 main.py 未注册 AssistantService，旧 routes.py 也未调用 Presenter。

## 4. 两者差异

| 维度 | CommunityOperationsAssistant | RuntimeAgentService |
|---|---|---|
| 输入 | 原始文本 + SessionContext + history | 已解析 RuntimeContext + TaskContext |
| 理解 | Agent loop 隐式选择工具 | Understanding 产出 IntentSpec/TaskIntent |
| 规划 | 无 TaskPlan | TaskOrchestrator 生成 TaskPlan |
| 执行状态 | run_store/旧 events | PlanExecution/ExecutionStateManager |
| 主 ID | run_id | execution_id；run_id 仅兼容 |
| 工具 | route tool_handler + 旧 schema | CapabilityExecutor + ArgumentBinder + ToolRuntime |
| 长任务 | 可能阻塞 HTTP | detach/AsyncTaskHandle/callback |
| 事件 | list[dict] {event,data} | ExecutionEventStore/ExecutionEvent |
| 失败 | HTTP error 与文本分离 | RuntimeResult FAILED + error_code |
| 产物 | 旧 tool data/文本 | RuntimeResult.artifacts + Presenter |
| approval | approval_store/session | HumanInteraction + ExecutionStateManager |
| 输出 | dict + RunAcceptedResponse | RuntimeResult，再投影 AssistantResponse |

因此不能把 body.content 直接传给 RuntimeAgentService。

## 5. 是否需要 RuntimeAdapter

结论：需要 API 层 ConversationRuntimeAdapter，不新增 execution 状态模型。

建议位置：

~~~text
apps/assistant_api/greenbook_assistant_api/services/conversation_runtime_adapter.py
~~~

职责：

1. 接收 conversation_id、AuthContext、MessageCreateRequest 和 app.state 依赖。
2. 读取并过滤公开 conversation history。
3. 调用正式 Understanding 入口，得到 IntentSpec，并保留 TaskIntent 兼容投影。
4. 解析或创建 Task，构造 TaskContext。
5. 组装不依赖 HTTP Request 的 RuntimeContext。
6. 调用 app.state.runtime_agent_service。
7. 用现有 RunExecutionAdapter 建立 run_id ↔ execution_id link。
8. 将 RuntimeResult 交给 ExecutionProjectionAdapter/Presenter。
9. 返回兼容 RunAcceptedResponse，并以新增字段方式提供 execution_id。
10. 在 completion_callback 中更新用户可见 message projection。

Adapter 不负责：

- 重写 Planner、Worker、ToolRuntime 或 StateManager。
- 直接调用 MCP。
- 直接修改 PlanExecution/StepExecution。
- 从 assistant_runs 推断 Runtime 状态。
- 任意 Runtime 失败后自动再次执行 Legacy。
- 用 LLM 成功文案覆盖 RUNNING/FAILED。

### 5.1 IntentSpec 的当前缺口

正式架构定义：

~~~text
User -> TaskUnderstanding -> IntentSpec -> Validator -> PlanningContext -> Planner
~~~

但当前 TaskUnderstanding.understand() 的返回类型是 TaskIntent。只有 Direct L2 成功时，TaskIntent.intent_spec 才有 IntentSpec 快照；简单 L1 输入可能没有。

对目标输入：

~~~text
帮我写一篇AI Agent学习路线帖子
~~~

当前无 LLM 的 L1 事实结果：

~~~json
{
  "relation": "NEW_TASK",
  "goal_category": "CREATE_CONTENT",
  "requirements": [{"type": "CREATE"}],
  "resource_requests": [{"operation": "CREATE", "resource_type": "CONTENT_DRAFT"}],
  "intent_spec": null
}
~~~

因此实现前必须选择：

- 优先：调用正式 Direct IntentSpec 入口，并保证简单请求也产出经过 schema/validator 校验的最小 IntentSpec。
- 兼容：在 adapter 只做受控的 L1 TaskIntent → IntentSpec 投影，再经 IntentValidator 校验。
- 禁止：把旧 IntentDraft/IntentElements 重新接回正式 L2 主路径，或让 RuntimeAgentService 重复理解文本。

### 5.2 TaskContext 与 Task provider 缺口

优先复用现有 IntentCompiler：

~~~python
task_context = IntentCompiler().compile(
    intent_spec=intent_spec,
    task_intent=task_intent,
    task=resolved_task,
    target_context=resolved_target,
    conversation=conversation_binding,
    timezone=body.timezone,
)
~~~

当前 IntentCompiler 要求 task 不为空；main.py 没有注册 TaskRegistry/TaskRepository，也没有持久 Task provider。因此：

- NEW_TASK 的最小 E2E 可以用现有 Task 模型创建带 user/tenant/conversation 的 Task 快照。
- CONTINUE_TASK/MODIFY_TASK 必须先接入现有 TaskRepository/TaskRegistry 或等价 provider。
- message_store 不是 Task 状态。
- 不能在 RuntimeAgentService 内隐式生成 Task 来绕过 TASK_REQUIRED。

这是一项实现前置条件，不应被 route 静默掩盖。

## 6. Adapter 字段映射

### 6.1 输入映射

| 字段 | 来源 | Runtime 映射 | 约束 |
|---|---|---|---|
| conversation_id | URL path | RuntimeContext、Task、SessionContext | 先校验 owner |
| user_id | AuthContext.user_id | RuntimeContext、Task、Tool auth | 不能来自 body/LLM |
| message | MessageCreateRequest.content | RuntimeContext.user_message、Understanding 输入 | 保留原文 |
| history | message_store 的公开 user/assistant 项 | RuntimeContext.conversation_history | 排除 tool/reasoning |
| timezone | body 或 session | RuntimeContext/TaskContext/ArgumentBinder | 相对时间由 Runtime binding |
| intent | Understanding 的 IntentSpec + TaskIntent | TaskContext.task_intent、PlanningContext.intent_spec | 保留 Spec 快照 |
| task_context | IntentCompiler + Task/Target | RuntimeContext.task_context | 必须有 task_id |
| execution_id | Runtime 创建 PlanExecution 后分配 | RuntimeResult.execution_id | 不得等同 run_id |
| runtime_result | execute() 或 completion callback | Presenter、兼容响应、message projection | 失败不可伪装成功 |

其余 RuntimeContext 注入：

- session：conversation_store 快照
- mcp：app.state.mcp
- llm/model：app.state.llm/model
- auth：request.state.auth_context
- active_draft_id/active_schedule_id：session/TaskContext 绑定
- trace_id/run_id：adapter 生成

### 6.2 Plan、Step、Artifact 映射

Adapter 不复制第二份 PlanExecution：

~~~text
IntentSpec + TaskContext
  -> RuntimeAgentService
  -> TaskPlan.plan_id
  -> PlanExecution.execution_id
  -> ExecutionStateManager/Repository/EventStore
~~~

计划和步骤通过 execution_id 查询；artifact 通过 RuntimeResult/Presenter 展示。旧 tool events 不得用于推断新的 step 状态。

### 6.3 ID 关系与兼容链接

~~~text
run_id        = conversation turn 兼容 ID
execution_id  = PlanExecution 唯一执行 ID
agent_run_id  = MCP/audit 上下文 ID
creator_task_id = Creator 自己的任务 ID
~~~

复用已有：

~~~python
RunExecutionAdapter.bind_run_execution(
    run_id,
    execution_id,
    conversation_id=conversation_id,
    task_id=task_id,
)
~~~

RunExecutionAdapter 只保存 ID link，不拥有 Runtime 状态。

### 6.4 初始/最终响应

推荐 detached 流程：

~~~text
request
  -> 生成 run_id
  -> execute(ctx, detach=True)
  -> 立即得到 RUNNING + execution_id
  -> HTTP 202 + run_id + execution_id + execution stream URL
  -> completion_callback 得到最终 RuntimeResult
  -> Presenter 生成 AssistantResponse
  -> 写入公开 message projection
~~~

规则：

- RUNNING：success=false，不得写“已完成”。
- COMPLETED：才允许完成型消息。
- FAILED：保留 execution_id、error_code、error_message。
- WAITING_APPROVAL：保留 approval_id 和 approval_required。
- 新 Runtime UI 使用 /api/v1/executions/{execution_id}/...
- 旧 /assistant/runs/{run_id}/... 仅做 link 兼容。

## 7. 如何保证旧链路可回滚

### 7.1 显式入口开关

建议在 message adapter 外层使用：

~~~text
ASSISTANT_MESSAGE_EXECUTION_PATH=legacy | runtime
~~~

| 配置 | 入口 |
|---|---|
| legacy | 现有 CommunityOperationsAssistant |
| runtime | ConversationRuntimeAdapter -> RuntimeAgentService |
| 未配置 | 初次部署建议保持 legacy，验证后再切 runtime |

禁止隐式双跑。

### 7.2 禁止自动 Runtime→Legacy fallback

以下情况不得自动重新执行 Legacy：

- IntentSpec 校验失败
- Planner/PlanValidator 失败
- Tool 参数错误
- Creator/Java 失败
- 已生成 execution_id
- 已写入任意 execution event
- 已有外部副作用

否则 create_draft、publish、schedule 可能重复执行。Runtime 失败应通过 RuntimeResult/API 体现，不得换执行器掩盖。

只有 Runtime 尚未创建 PlanExecution 且部署明确标记为兼容回退时，才可人工切回 Legacy；默认应返回明确错误。

### 7.3 保持旧响应与读模型

- 保留 run_id、conversation_id、status、events_url。
- 以新增字段方式增加 execution_id、execution_url/stream URL。
- 旧 /assistant/runs/{run_id} 通过 RunExecutionLink 查询 Runtime execution。
- Runtime-backed status/steps/events 从 PlanExecution/EventStore 读取。
- run_store/assistant_runs 仅保存兼容 metadata 或 LEGACY_ONLY。
- Runtime FAILED 不得投影成 COMPLETED 文案。
- message projection 使用 RuntimeResult/Presenter，不从旧状态覆盖 Runtime。

### 7.4 回滚操作

~~~text
1. 将 ASSISTANT_MESSAGE_EXECUTION_PATH 切回 legacy
2. 保留已有 PlanExecution 和 EventStore 数据
3. 已创建的 Runtime execution 继续由 Runtime API 查询/恢复
4. 新请求重新走旧 send_message
5. 不回滚 Planner、Worker、ToolRuntime、StateManager 或数据库
~~~

代码回滚只需撤销 adapter 接线或配置，不删除 Runtime 数据。

## 8. 目标用例验证设计

输入：

~~~text
帮我写一篇AI Agent学习路线帖子
~~~

预期语义：

~~~text
TaskUnderstanding
  -> IntentSpec(mode=SIMPLE, action=CREATE, resource=CONTENT)
  -> TaskIntent(relation=NEW_TASK, goal_category=CREATE_CONTENT, requirements=[CREATE])
  -> TaskContext(task_id, goal, task_intent)
~~~

预期计划和执行：

~~~text
TaskContext
  -> TaskOrchestrator
  -> TaskPlan(template=SINGLE_CREATE, plan_id)
  -> PlanValidator
  -> PlanExecution(execution_id)
  -> GENERATE_CONTENT
  -> VALIDATE_QUALITY
~~~

步骤数量必须读取当前 Planner/registry 结果，不能由 adapter 自己硬编码。

验证要点：

~~~python
result = await adapter.handle_message(...)
assert result.intent_spec is not None
assert result.task_context.task_id
assert result.runtime_result.execution_id

execution = app.state.execution_repository.find_by_id(
    result.runtime_result.execution_id
)
assert execution is not None
assert execution.plan_id
assert execution.task_id == result.task_context.task_id
assert {step.capability for step in execution.steps} >= {
    "GENERATE_CONTENT",
    "VALIDATE_QUALITY",
}
~~~

detach=True 时，初始只断言：

- status=RUNNING
- success=False
- execution_id 非空
- PlanExecution 已存在

随后通过 callback 或 Execution API 等待最终 COMPLETED/FAILED。

### 8.1 回归测试建议

新增：

~~~text
tests/unit/test_conversation_runtime_adapter.py
tests/contract/test_message_runtime_migration.py
tests/e2e/test_message_runtime_path.py
~~~

覆盖：

1. conversation_id/user_id/message/history/timezone/auth 映射。
2. IntentSpec 与 TaskContext 同时存在。
3. RuntimeAgentService 被调用，CommunityOperationsAssistant 未被调用。
4. TaskPlan/PlanExecution/plan_id/steps/execution_id 来自 canonical Runtime。
5. run_id 与 execution_id 不相等但存在 link。
6. RUNNING 不生成成功文案。
7. FAILED 带 error_code/error_message，不能变 COMPLETED。
8. WAITING_APPROVAL 保留 approval_id。
9. legacy 开关保持原响应。
10. Runtime 失败后不自动重复 Legacy。
11. 新 execution API 和兼容 run API 指向同一 execution。
12. 不以 assistant_runs 作为状态来源。

## 9. 实施边界与前置决策

Step2-B 实现应限于：

- 新增 ConversationRuntimeAdapter。
- 修改 message endpoint 的选择/委托。
- 必要的兼容响应字段、RunExecutionLink 投影。
- adapter、contract、E2E 测试。

不修改：

- Planner、Worker、ToolRuntime、CapabilityExecutor。
- ExecutionStateManager、PlanExecution、ExecutionEventStore。
- 数据库 schema/migration。
- CommunityOperationsAssistant 的实现。
- 旧 routes 的其他 endpoint。
- 第二套 execution 状态模型。

实现前必须确认：

1. 简单 L1 请求如何正式产出 IntentSpec。
2. TaskRepository/TaskRegistry 的 Task provider。
3. detached 202 还是短任务同步策略。
4. compatibility response 是否新增 execution_id。
5. RunExecutionAdapter 的 provider/lifespan 接线。
6. completion_callback 的 message projection 生命周期。
7. Presenter 作为统一用户回复出口。

## 10. 最终判断

当前 message 入口仍是 Legacy Agent 入口；Runtime HTTP API 已存在但未接管聊天请求。

要满足：

~~~text
用户一句话
  -> IntentSpec
  -> TaskContext
  -> RuntimeAgentService
  -> TaskPlan
  -> PlanExecution
  -> execution_id
~~~

必须在 API 边界增加 ConversationRuntimeAdapter，并明确处理 IntentSpec formalization、Task/Target 解析、RuntimeContext 装配、run/execution link、detached 202、Presenter 和不可重复执行的回滚策略。

本文件只完成 Step2-B 设计分析，不代表代码已迁移。
