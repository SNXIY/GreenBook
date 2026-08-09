# GreenBook Agent Runtime — RuntimeAdapter 详细设计

> 日期: 2026-08-07
> 状态: 设计完成 — 等待编码
>
> 本文档定义 RuntimeAdapter 层的完整接口设计：
> Service 层隔离、RuntimeContext、RuntimeResult、调用链、fallback 策略。

---

# 1. 当前 send_message 结构分析

## 1.1 send_message 六个阶段

```
send_message()  (~440 行)
│
├── Phase A: 请求准备 (行 799-825)
│   Auth → SessionContext → MCP/LLM → trace_id/run_id
│   → 加载历史 → 保存 user message
│
├── Phase B: TaskUnderstanding (行 827-862)
│   TaskUnderstanding.understand() → TaskIntent
│   TaskResolver.resolve_target() → 填充 target_task_id
│
├── Phase C: 回调定义 (行 864-949)
│   emit_event, tool_handler, on_tool_start, on_tool_complete,
│   on_assistant_delta
│
├── Phase D: Agent 执行 (行 1041-1062)
│   CommunityOperationsAssistant(…)
│   assistant.run(…)
│
├── Phase E: 错误处理 (行 1063-1179)
│   Exception → MODEL_REQUEST_FAILED
│   tool_failure → PARTIAL_FAILURE / RUN_FAILED
│
└── Phase F: 成功处理 (行 1181-1207)
    _save_session → 保存 assistant message → _create_run
    → TaskRegistry.create_task → save_intent → 202 响应
```

## 1.2 哪些属于 HTTP 层（保留在 routes.py）

| 逻辑 | 位置 | 理由 |
|------|------|------|
| JWT 认证 | `_get_auth()` | HTTP 中间件职责 |
| Session 加载/保存 | `_load_session()` / `_save_session()` | 与 Request 绑定 |
| 消息持久化 | `repos.messages.add()` | DB 操作,属于 HTTP 层 |
| SSE 事件发射 | `emit_event()` | 前端协议 |
| HTTP 错误映射 | `_http_status_for_tool_error()` | 前端协议 |
| 审批流 | `_create_approval()` / `approve_operation()` | 前端交互 |

## 1.3 哪些属于 Service 层（可从 routes.py 提取）

| 逻辑 | 位置 | 理由 |
|------|------|------|
| TaskUnderstanding | `tu.understand()` | 业务逻辑,不依赖 HTTP |
| TaskResolver | `resolve_target()` | 纯数据匹配 |
| Agent 执行 | `agent.run()` | 核心业务逻辑 |
| 工具调用 | `tool_handler` 回调 | 核心业务逻辑 |
| 执行结果处理 | `successful_draft/schedule/…` | 业务状态 |
| Task 持久化 | `TaskRegistry.create_task()` | 业务数据 |

---

# 2. Service 层设计

## 2.1 三层架构

```
┌─────────────────────────────────────────────────────────────────┐
│ routes.py: send_message()          ← HTTP 层                   │
│   职责: Auth, Session, SSE事件, HTTP错误映射, 前端响应          │
│   不包含: Agent 执行逻辑, Task 管理逻辑                        │
└─────────────────────────┬───────────────────────────────────────┘
                          │ 调用
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│ AssistantService                   ← 编排层                    │
│   职责:                                                         │
│   1. 创建 RuntimeContext                                        │
│   2. 选择执行路径 (Legacy vs Runtime)                           │
│   3. 调用具体 Service                                           │
│   4. 处理 fallback                                              │
│   5. 返回统一 RuntimeResult                                     │
└────────┬──────────────────────────────────┬─────────────────────┘
         │                                  │
         ▼                                  ▼
┌──────────────────────────┐  ┌──────────────────────────────────┐
│ LegacyAgentService       │  │ RuntimeAgentService              │
│                          │  │                                  │
│  包装旧 agent.py         │  │  组装新 Runtime 管线:            │
│                          │  │  TaskUnderstanding               │
│  CommunityOperations     │  │  → Orchestrator                  │
│    Assistant.run()       │  │  → Worker                        │
│                          │  │  → CapabilityExecutor            │
│                          │  │  → ToolRuntime                   │
└──────────────────────────┘  └──────────────────────────────────┘
```

## 2.2 AssistantService 接口

```python
class AssistantService:
    """编排层 — 选择执行路径, 处理 fallback, 返回统一结果."""

    def __init__(
        self,
        legacy: LegacyAgentService,
        runtime: RuntimeAgentService | None = None,
        mode: str = "off",     # off | dual | on
    ): ...

    async def execute(
        self,
        ctx: RuntimeContext,
    ) -> RuntimeResult:
        """
        1. 根据 mode + ctx.task_intent 选择路径
        2. 执行
        3. 执行失败时 fallback
        """
```

## 2.3 LegacyAgentService

```python
class LegacyAgentService:
    """包装旧 agent.py, 接口与新 Runtime 对齐."""

    def __init__(self, llm, model, mcp): ...

    async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
        """
        内部:
        1. 构建 tool_handler (复用 MCP)
        2. CommunityOperationsAssistant(…)
        3. assistant.run(user_message, session, tool_handler, …)
        4. 将旧 agent.run() 的返回 dict → RuntimeResult
        """
```

## 2.4 RuntimeAgentService

```python
class RuntimeAgentService:
    """组装新 Runtime 管线."""

    def __init__(
        self,
        registry: CapabilityRegistry,
        orchestrator: TaskOrchestrator,
        validator: PlanValidator,
        worker: ExecutionWorker,
        artifact_store: ArtifactStore,
        collector: TraceCollector,
    ): ...

    async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
        """
        1. CapabilityMapper.capabilities_for_goal(goal_category)
        2. Orchestrator.generate_plan(requirements)
        3. Validator.validate(plan)
        4. Worker.init_from_plan(executable)
        5. Worker.run(execution_id)
        6. 收集 artifacts + trace
        7. 构建 RuntimeResult
        """
```

---

# 3. RuntimeContext 设计

```python
@dataclass
class RuntimeContext:
    """一次 Agent 执行的完整上下文.

    由 routes.py 在 Phase A+B 构建, 传递给 AssistantService.
    Service 层不访问 Request/HTTP 对象.
    """

    # ── 标识 ──
    conversation_id: str
    run_id: str
    trace_id: str
    task_id: str = ""            # 解析后的 Task ID (Phase 2.5)
    execution_id: str = ""       # Worker 分配

    # ── 用户 ──
    user_id: str
    tenant_id: str
    timezone: str = "Asia/Shanghai"

    # ── 输入 ──
    user_message: str
    conversation_history: list[dict[str, str]]  # [{role, content}, …]

    # ── 理解结果 ──
    task_intent: TaskIntent | None = None

    # ── 会话快照 ──
    session: SessionContext | None = None
    active_draft_id: str | None = None
    active_schedule_id: str | None = None

    # ── 注入依赖 (Service 内部使用) ──
    mcp: Any = None              # GreenBookMCPServer
    llm: Any = None              # AsyncOpenAI
    model: str = ""              # LLM model name
    db_session: Any = None       # PostgreSQL AsyncSession
```

### RuntimeContext 生命周期

```
routes.py:send_message()
  │
  ├── Phase A: 构建基础 RuntimeContext
  │   ctx = RuntimeContext(
  │     conversation_id=…, run_id=…, trace_id=…,
  │     user_id=…, tenant_id=…, timezone=…,
  │     user_message=body.content,
  │     conversation_history=…,
  │     session=session,
  │     mcp=mcp, llm=llm, model=model, db_session=…,
  │   )
  │
  ├── Phase B: 填充 Task 相关字段
  │   ctx.task_intent = task_intent
  │   ctx.task_id = task_intent.target_task_id or ""
  │
  └── Phase D: 传递给 Service
      result = await assistant_service.execute(ctx)
```

---

# 4. RuntimeResult 设计

```python
@dataclass
class RuntimeResult:
    """一次 Agent 执行的统一返回.

    无论走 Legacy 还是 Runtime 路径, 都返回此结构.
    routes.py 根据此结构构建 HTTP 响应.
    """

    # ── 基本状态 ──
    success: bool = False
    status: str = ""             # COMPLETED | FAILED | WAITING_APPROVAL | PARTIAL_FAILURE
    run_id: str = ""
    task_id: str = ""

    # ── 用户可见 ──
    content: str = ""            # 给用户的自然语言响应
    summary: str = ""            # 简短摘要

    # ── 执行元数据 ──
    started_execution: bool = False   # Worker 是否实际开始执行
    side_effect_committed: bool = False  # 是否有外部写入已提交
    fallback_allowed: bool = True      # 是否允许回退到 Legacy
    execution_path: str = ""           # "legacy" | "runtime"

    # ── 错误 ──
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False

    # ── SSE 事件 ──
    events: list[dict] = field(default_factory=list)

    # ── 产物 ──
    draft_id: str | None = None
    schedule_id: str | None = None
    artifact_ids: list[str] = field(default_factory=list)

    # ── 追踪 ──
    trace_id: str = ""
    tool_rounds: int = 0
    duration_ms: float = 0.0
```

### RuntimeResult → HTTP 映射

```python
# routes.py 中的映射:

def _build_response(result: RuntimeResult) -> RunAcceptedResponse:
    return RunAcceptedResponse(
        run_id=result.run_id,
        conversation_id=…,
        status=result.status,
        events_url=f"/api/v1/assistant/runs/{result.run_id}/events",
    )

def _build_error_response(result: RuntimeResult) -> HTTPException:
    if result.status == "WAITING_APPROVAL":
        return …  # 不抛异常, 正常返回 202
    return HTTPException(
        status_code=_http_status_for_tool_error(result.error_code),
        detail={
            "code": result.error_code,
            "message": result.error_message,
            "run_id": result.run_id,
            "events_url": …,
        },
    )
```

---

# 5. 调用链

## 5.1 新 Runtime 路径

```
POST /conversations/{id}/messages
    │
    ▼
routes.py:send_message()                         # HTTP 层
    │
    ├── Phase A: 构建 RuntimeContext
    │   Auth → Session → History → RuntimeContext
    │
    ├── Phase B: TaskUnderstanding (旁路 → 变为主路)
    │   tu.understand() → TaskIntent
    │   resolve_target() → target_task_id
    │
    ├── Phase C: 构建 Service
    │   assistant_service = AssistantService(legacy, runtime)
    │
    ├── Phase D: 执行
    │   result = await assistant_service.execute(ctx)
    │
    └── Phase F: 响应
        _save_session()
        await repos.messages.add(…, result.content)
        await _create_run(…, result.events, result.status)
        await task_registry.create_task(…)
        return 202 RunAcceptedResponse

───────────────────────────────────────────────────
Service 层内部 (routes.py 不感知):
───────────────────────────────────────────────────

AssistantService.execute(ctx)
    │
    ├── [mode=off 或 goal_category 不支持]
    │   → legacy.execute(ctx)
    │
    └── [mode=dual 且 goal_category 在支持列表]
        → runtime.execute(ctx)
            │
            ├── 1. CapabilityMapper.capabilities_for_goal(goal_category)
            │
            ├── 2. Orchestrator.generate_plan(task_id, goal_category, requirements)
            │      → TaskPlan (template: CREATE_AND_PUBLISH / SINGLE_IMPROVE / SINGLE_SEARCH)
            │
            ├── 3. Validator.validate(plan)
            │      → ExecutablePlan
            │
            ├── 4. Worker.init_from_plan(executable, task_id)
            │      → PlanExecution
            │
            ├── 5. Worker.run(execution_id)
            │      │
            │      ├── Step 1: GENERATE_CONTENT
            │      │   CapabilityExecutor → ToolRuntime → tool_handler → mcp → Java/Creator
            │      │
            │      ├── Step 2: VALIDATE_QUALITY (LLM auto-success)
            │      │
            │      └── Step 3: SCHEDULE_PUBLISH
            │          CapabilityExecutor → ToolRuntime → tool_handler → mcp → Java
            │
            └── 6. 构建 RuntimeResult
                content = "已创建草稿…"
                draft_id = …  schedule_id = …
                events = [trace events → SSE 格式]
```

## 5.2 Fallback 到旧路径

```
AssistantService.execute(ctx)
    │
    ├── runtime.execute(ctx)
    │   │
    │   ├── 正常 → 返回 RuntimeResult
    │   │
    │   └── 异常!
    │       ↓
    └── [ctx.fallback_allowed = True]
        → legacy.execute(ctx)
            │
            ├── 正常 → 返回 RuntimeResult (execution_path="legacy")
            │
            └── 异常 → 返回 RuntimeResult(success=False, error_code=…)
```

---

# 6. Fallback 策略

## 6.1 触发条件

| 条件 | 动作 |
|------|------|
| Runtime 管线抛异常 | 自动回退 Legacy, 记录 WARN 日志 |
| Orchestrator 无匹配模板 | 回退 Legacy |
| PlanValidator 校验失败 | 回退 Legacy |
| Worker.run() 返回 FAILED | 不 fallback — Runtime 的失败是正常结果 |
| CapabilityExecutor 返回 UNKNOWN_CAPABILITY | 不 fallback — 返回错误给用户 |
| ToolRuntime 超时 | 不 fallback — Runtime 内重试 |

## 6.2 不回退的场景

以下场景 Runtime 路径失败是**预期行为**, 不回退到 Legacy:

- 用户请求超出 Assistant 能力范围
- 外部服务 (Java/Creator) 不可用
- 审批暂停 (WAITING_APPROVAL)
- 参数校验失败

## 6.3 回退的副作用

```
Runtime 路径已执行的副作用 → 回退到 Legacy 时需要处理:

情况 A: Runtime 尚未执行任何 Step
  → 安全回退, Legacy 从头执行

情况 B: Runtime 已执行 Step 1 (如 SEARCH)
  → Legacy 重新执行, 重复搜索 (无副作用, 可接受)

情况 C: Runtime 已执行 CREATE (创建了草稿)
  → Legacy 可能再次创建草稿 (重复草稿, 需清理)
  → Phase 4.5 策略: 回退时标记 side_effect_committed=True,
    Legacy 复用已创建的 draft_id 而非重新创建

情况 D: Runtime 已执行 SCHEDULE (创建了定时任务)
  → 同情况 C, Legacy 复用已创建的 schedule_id
```

## 6.4 降级开关

```python
# 环境变量控制
ASSISTANT_RUNTIME_MODE=off    # 全部走 Legacy
ASSISTANT_RUNTIME_MODE=dual   # 3 个场景走 Runtime, 其余 Legacy
ASSISTANT_RUNTIME_MODE=on     # 全部走 Runtime (Phase 5 目标)

# 运行时切换 (计划内)
# 未来支持 per-conversation / per-user feature flag
```

---

# 7. 文件变更清单

## 新增 (3)

```
apps/assistant_api/greenbook_assistant_api/services/
    __init__.py                  (~5 行)
    assistant_service.py         (~80 行)  — AssistantService (编排)
    legacy_agent_service.py      (~90 行)  — LegacyAgentService (包装旧agent)
    runtime_agent_service.py     (~150 行) — RuntimeAgentService (新管线)

apps/assistant_api/greenbook_assistant_api/models/
    runtime_context.py           (~50 行)  — RuntimeContext
    runtime_result.py            (~60 行)  — RuntimeResult
```

## 修改 (2)

```
apps/assistant_api/main.py
  + 初始化 AssistantService (包装 legacy + runtime)
  + app.state.assistant_service = AssistantService(…)

apps/assistant_api/api/routes.py
  ✂️ send_message() 中 Phase D/E/F 提取到 AssistantService
  + result = await request.app.state.assistant_service.execute(ctx)
  + _build_response(result) / _build_error_response(result)
  预计减少 ~200 行
```

## 不修改 (全部)

```
agent.py                    — 零改动
packages/assistant_core/    — 零改动 (所有新模块已就绪)
services/greenbook_mcp/     — 零改动
packages/java_client/       — 零改动
packages/creator_client/    — 零改动
creator-agent/              — 零改动
```

---

# 8. 简化后的 routes.py

```
send_message() 重构后 (~240 行, 从 ~440 行减少):

async def send_message(conversation_id, body, request):
    # ── Phase A: 准备 ──
    auth, session, mcp, llm, model, trace_id, run_id = _prepare(request, conversation_id, body)
    repos = _Repos(request)
    history = await repos.messages.find_by_conversation(…)
    await repos.messages.add(…, "user", body.content)

    # ── Phase B: Task 理解 ──
    task_intent, task_summaries = await _understand_intent(request, body, conversation_id, auth)

    # ── Phase C: 构建 RuntimeContext ──
    ctx = RuntimeContext(
        conversation_id=conversation_id, run_id=run_id, trace_id=trace_id,
        user_id=auth.user_id, tenant_id=auth.tenant_id, timezone=session.timezone,
        user_message=body.content, conversation_history=history,
        task_intent=task_intent, session=session,
        mcp=mcp, llm=llm, model=model, db_session=repos._db_session,
    )

    # ── Phase D: 执行 ──
    service = request.app.state.assistant_service
    result = await service.execute(ctx)

    # ── Phase E: 持久化响应 ──
    await _save_session(request, session)
    if result.content:
        await repos.messages.add(conversation_id, "assistant", result.content)
    await _create_run(request, run_id=…, status=result.status, events=result.events, …)

    # ── Phase F: HTTP 响应 ──
    if not result.success and result.status != "WAITING_APPROVAL":
        raise _build_error_response(result)
    return _build_response(result)
```
