# GreenBook Agent Runtime — 接入现有 API 迁移方案

> 日期: 2026-08-07
> 状态: 设计完成 — 等待执行
>
> 将 Phase 0–4.4 构建的新 Runtime 接入现有 `apps/assistant_api`，
> 旧 agent.py 保留为 fallback，先支持 3 个真实场景。

---

# 1. 集成架构

## 1.1 新旧路径对比

```
当前路径 (旧 agent.py):
  routes.py:send_message()
    → TaskUnderstanding (旁路,不改变执行)
    → agent.py:CommunityOperationsAssistant.run()
      → _turn_intents() → _turn_routing_hint() → _turn_tool_filter()
      → LLM tool calling loop
      → tool_handler → mcp.execute_tool() → MCP → Java/Creator

新增路径 (新 Runtime):
  routes.py:send_message()
    → [feature flag 判断]
    → TaskUnderstanding → TaskIntent
    → CapabilityMapper → Capability list
    → TaskOrchestrator → TaskPlan
    → PlanValidator → ExecutablePlan
    → ExecutionWorker → Step execution
      → CapabilityExecutor → ToolRuntime → tool_handler → mcp → Java/Creator
```

## 1.2 关键决策: 新旧并存

```
send_message()
    │
    ├── [feature flag = off 或 goal_category = DIRECT/QUERY_INFO]
    │   → 旧 agent.py 路径 (现有一字不改)
    │
    └── [feature flag = on 且 goal_category 可被 Runtime 处理]
        → 新 Runtime 路径
```

**回退策略:** 新路径任何异常 → 自动回退到旧 agent.py。

**灰度策略:** 通过环境变量 `ASSISTANT_RUNTIME_MODE=dual` 控制。
- `off`: 全部走旧路径
- `dual`: 3 个场景走新路径,其余走旧路径
- `on`: 全部走新路径 (Phase 5 目标)

---

# 2. 新增文件

```
apps/assistant_api/greenbook_assistant_api/runtime/
    __init__.py              (~10 行)
    adapter.py               (~120 行) — RuntimeAdapter, 桥接新旧
    pipeline.py              (~150 行) — 组装完整 Runtime 管线
```

## 2.1 runtime/adapter.py

**职责:** 将现有 MCP/Jav/Creator 基础设施包装为新 Runtime 可用的接口。

```
RuntimeAdapter:
  ├── tool_handler(tool_name, tool_args) → dict
  │     封装 routes.py 中的 tool_handler 回调逻辑:
  │     - 名称转换 (_ → .)
  │     - _bind_target_tool_args
  │     - schedule 时间标准化
  │     - community_references 注入
  │     - requires_approval 检查
  │     - mcp.execute_tool() 调用
  │
  ├── llm_client → AsyncOpenAI
  │     直接复用 app.state.llm
  │
  ├── model → str
  │     直接复用 app.state.model
  │
  └── auth_context → AuthContext
        从 request.state.auth_context 获取
```

## 2.2 runtime/pipeline.py

**职责:** 组装完整的 Runtime 管线,暴露单一的 `run()` 入口。

```
class RuntimePipeline:
    """一站式 Runtime 执行管线."""

    def __init__(self, adapter: RuntimeAdapter, db_session):
        self.task_understanding = TaskUnderstanding(adapter.llm, adapter.model)
        self.capability_mapper = CapabilityMapper()
        self.orchestrator = TaskOrchestrator()
        self.validator = PlanValidator()
        self.tool_runtime = ToolRuntime(adapter.tool_handler)
        self.capability_executor = CapabilityExecutor(registry, tool_runtime)
        self.worker = ExecutionWorker(capability_executor)
        self.artifact_store = ArtifactStore()
        self.registry = TaskRegistry(db_session)
        self.collector = TraceCollector()

    async def run(
        self,
        user_message: str,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> RuntimeResult:
        """完整管线执行."""
```

---

# 3. 修改文件

## 3.1 apps/assistant_api/main.py

```diff
变更点:
  1. lifespan 中新增 RuntimePipeline 初始化:

+ from greenbook_assistant_api.runtime.adapter import RuntimeAdapter
+ from greenbook_assistant_api.runtime.pipeline import RuntimePipeline
+
+ app.state.runtime_mode = os.getenv("ASSISTANT_RUNTIME_MODE", "off")
+ if app.state.runtime_mode != "off":
+     app.state.adapter = RuntimeAdapter(…)
+     app.state.runtime_pipeline = RuntimePipeline(
+         adapter=app.state.adapter,
+         db_session=app.state.db,
+     )

影响行数: +15
```

## 3.2 apps/assistant_api/api/routes.py

```diff
变更点:
  1. send_message() 中新增 Runtime 路径分流:

  # 现有 TaskUnderstanding 代码之后, agent.run() 之前:

+ runtime_mode = getattr(request.app.state, "runtime_mode", "off")
+ runtime_pipeline = getattr(request.app.state, "runtime_pipeline", None)
+
+ if runtime_mode != "off" and runtime_pipeline is not None and task_intent is not None:
+     # 只对 3 个受支持场景走新路径
+     if _should_use_runtime(task_intent):
+         try:
+             result = await runtime_pipeline.run(
+                 user_message=body.content,
+                 conversation_id=conversation_id,
+                 user_id=auth.user_id,
+                 tenant_id=auth.tenant_id,
+                 session=session,
+                 task_intent=task_intent,
+                 recent_tasks=task_summaries,
+             )
+             # result 已包含 content + events, 直接跳到响应处理
+             goto SUCCESS_PATH
+         except Exception:
+             logger.exception("Runtime pipeline failed, falling back to legacy")
+             # 回退到旧路径
+
+ # 旧路径 (不变)
  assistant = CommunityOperationsAssistant(…)

  2. 新增辅助函数:

+ def _should_use_runtime(task_intent: TaskIntent) -> bool:
+     """Phase 4.5: 只对 3 个场景启用新 Runtime."""
+     SUPPORTED = {
+         ("CREATE_CONTENT", "NEW_TASK"),       # 场景 1
+         ("IMPROVE_CONTENT", "MODIFY_TASK"),   # 场景 2
+         ("ANALYZE_COMMUNITY", "NEW_TASK"),    # 场景 3
+     }
+     return (task_intent.goal_category, task_intent.relation) in SUPPORTED

影响行数: +40
```

---

# 4. 3 个场景映射

## 场景 1: 创建帖子并定时发布

```
用户: "帮我写一篇Java入门文章，明天上午8点发布"

TaskUnderstanding → L1:
  relation=NEW_TASK
  goal_category=CREATE_CONTENT
  requirements=[{type:CREATE}, {type:PUBLISH}]

Runtime Pipeline:
  1. CapabilityMapper.capabilities_for_goal("CREATE_CONTENT")
     → [GENERATE_CONTENT]

  2. Orchestrator.generate_plan(requirements=[CREATE, PUBLISH])
     → CREATE_AND_PUBLISH template (3 steps)
       s1: GENERATE_CONTENT    → content.create_draft
       s2: VALIDATE_QUALITY    → (LLM)
       s3: SCHEDULE_PUBLISH    → publication.schedule

  3. Validator.validate(plan)
     → ExecutablePlan(is_valid=True)

  4. Worker.init_from_plan → PlanExecution

  5. Worker.run():
     s1: CapabilityExecutor → ToolRuntime → mcp.execute_tool("content.create_draft", …)
         → draft_id = "draft-123"
     s2: CapabilityExecutor (LLM skip) → VALIDATE_QUALITY auto-success
     s3: CapabilityExecutor → ToolRuntime → mcp.execute_tool("publication.schedule", …)
         → schedule_id = "sched-456"

  6. RuntimeResult(content="已创建草稿并设置明天8点发布", …)
```

## 场景 2: 修改已有帖子内容和时间

```
用户: "修改刚才Java文章的标题为Java从入门到精通，发布时间改为后天"

前提: 上一轮已创建 Task (goal="写一篇Java入门文章", artifacts=[DRAFT])

TaskUnderstanding → L1:
  relation=MODIFY_TASK
  goal_category=IMPROVE_CONTENT
  target_task_hint="Java文章"
  requirements=[{type:IMPROVE}, {type:PUBLISH}]

TaskResolver → target_task_id = Task 的 task_id

Runtime Pipeline:
  1. Orchestrator.generate_plan(requirements=[IMPROVE, PUBLISH])
     → SINGLE_IMPROVE template (1 step)
       s1: IMPROVE_CONTENT → content.revise_draft
     (发布需求由后续轮次或同一步骤的约束处理)

  2. Worker.run():
     s1: CapabilityExecutor → ToolRuntime → mcp.execute_tool("content.revise_draft", {
           draft_id: <从 Task.artifacts 解析>,
           revision_instruction: "标题改为…,发布时间改为后天",
         })
     → 修改成功

  注意: Phase 4.5 的 CREATIVE_AND_PUBLISH 模板只有 GENERATE→VALIDATE→SCHEDULE,
  对于 "修改+调整时间" 场景, Orchestrator 需要支持 IMPROVE + PUBLISH 组合。
  如果当前模板不支持, 回退到旧路径。
```

## 场景 3: 搜索社区帖子并总结

```
用户: "搜索社区Java帖子并总结热门内容"

TaskUnderstanding → L1:
  relation=NEW_TASK
  goal_category=ANALYZE_COMMUNITY
  requirements=[{type:SEARCH}]

Runtime Pipeline:
  1. Orchestrator.generate_plan(requirements=[SEARCH])
     → SINGLE_SEARCH template (1 step)
       s1: SEARCH_COMMUNITY → community.search_public_posts

  2. Worker.run():
     s1: CapabilityExecutor → ToolRuntime → mcp.execute_tool("community.search_public_posts", …)
     → 返回搜索结果 → LLM 总结 → 返回给用户
```

---

# 5. RuntimeResult 模型

```python
class RuntimeResult:
    """统一的新 Runtime 返回格式, 兼容旧 RunAcceptedResponse."""

    content: str                        # 给用户的自然语言响应
    tool_rounds: int                    # 执行的 tool 调用次数
    run_id: str
    task_id: str
    status: str                         # COMPLETED / FAILED / WAITING_APPROVAL
    events: list[dict]                  # SSE 事件列表 (兼容旧格式)
    error_code: str | None
    error_message: str | None
```

---

# 6. RuntimeAdapter 关键桥接

```python
class RuntimeAdapter:
    """
    将现有 MCP/Java/Creator 基础设施适配为新 Runtime 接口。

    不复制代码 — 直接复用 routes.py 中已有的 tool_handler 逻辑。
    """

    def __init__(self, request: Request):
        self.mcp = request.app.state.mcp
        self.llm = request.app.state.llm
        self.model = request.app.state.model
        self.auth = request.state.auth_context

    async def tool_handler(self, tool_name: str, tool_args: dict) -> dict:
        """
        签名为 (tool_name, tool_args) → dict,
        内部复用 mcp.execute_tool(), 处理:
        - 名称转换
        - 参数绑定 (session.active_*)
        - 时间标准化
        - 审批检查
        """
        # 复用 routes.py 的 tool_handler 逻辑
        mcp_name = tool_name  # CapabilityExecutor 已使用 dot 格式
        result = await self.mcp.execute_tool(
            mcp_name,
            auth=self.auth,
            session=self.session,
            trace_id=self.trace_id,
            agent_run_id=self.run_id,
            tool_call_id=str(uuid4()),
            **tool_args,
        )
        return result
```

---

# 7. 风险分析

| 风险 | 等级 | 缓解 |
|------|------|------|
| Runtime Pipeline 执行失败导致 500 | 🔴 | try/except 包裹, 异常回退到旧 agent.py |
| ToolRuntime 参数构建与旧 tool_handler 不一致 | 🟡 | RuntimeAdapter 直接复用 mcp.execute_tool(), 参数构建在 CapabilityExecutor 层 |
| Orchestrator 模板不匹配回退过多 | 🟡 | 回退到旧路径对用户透明, 仅影响内部 trace |
| 新 Runtime 增加延迟 | 🟢 | 仅 3 个场景走新路径, LLM 调用次数与旧路径相当 |
| 前端 API 兼容性 | 🟢 | RuntimeResult 格式与旧 RunAcceptedResponse 兼容 |

---

# 8. 执行步骤

## Step 1: 创建 RuntimeAdapter (1 天)

- 新增 `apps/assistant_api/greenbook_assistant_api/runtime/adapter.py`
- 包装 MCP + AuthContext 为 ToolRuntime 可用接口
- 单元测试: 验证 tool_handler 正确转发到 mcp.execute_tool()

## Step 2: 创建 RuntimePipeline (1 天)

- 新增 `apps/assistant_api/greenbook_assistant_api/runtime/pipeline.py`
- 组装: TaskUnderstanding → CapabilityMapper → Orchestrator → Worker → CapabilityExecutor → ToolRuntime
- 单元测试: Mock MCP, 验证管线端到端

## Step 3: routes.py 分流 (1 天)

- 修改 `routes.py:send_message()`
- 新增 `_should_use_runtime()` 判断
- 新旧路径 try/except 回退

## Step 4: 场景 1 E2E 验证 (1 天)

- "帮我写一篇Java文章，明天8点发布"
- 验证: TaskIntent 正确, Plan 生成 CREATE_AND_PUBLISH, Step 正确执行到 SCHEDULE_PUBLISH

## Step 5: 场景 2+3 E2E 验证 (1 天)

- 场景 2: 修改文章标题+时间
- 场景 3: 搜索+总结

## Step 6: 灰度上线 (1 天)

- 默认 `ASSISTANT_RUNTIME_MODE=off`
- 内部测试环境设为 `dual`
- 验证 1 周无异常后推广

---

# 9. 不在此 Phase 修改的文件

```
agent.py               — 零改动 (回退用)
MCP tool_registry.py   — 零改动
services/greenbook_mcp/ — 零改动
packages/java_client/  — 零改动
packages/creator_client/ — 零改动
creator-agent/         — 零改动
```
