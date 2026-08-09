# Phase 6.1 — GroupExecutor 设计

> 日期: 2026-08-07
> 前置: Phase 6.0.1 TaskDecomposer
> 状态: 设计阶段

---

# 1. RuntimeAgentService.execute() 结构分析

## 1.1 当前 10 步流程

```
execute(ctx: RuntimeContext) → RuntimeResult

  1. Capabilities check     ← ctx.task_intent.goal_category
  2. Resource Resolution    ← ctx.task_intent.resource_requests, ctx.recent_tasks
  3. Plan generation        ← ctx.task_intent.requirements
  4. Validation
  5. Observability setup    ← ctx.trace_id, ctx.run_id
  6. ToolRuntime setup      ← ctx.mcp, ctx.auth, ctx.session
  7. CapabilityExecutor     ← ctx.mcp
  8. Worker init + run
  9. Artifact collection
 10. Result build           ← ctx.run_id, ctx.trace_id
```

## 1.2 ctx 字段分类

| 分类 | 字段 | 复用策略 |
|------|------|---------|
| 共享设施 | llm, mcp, auth, db_session, timezone | 所有 SubTask 复用 |
| 会话级 | conversation_id, user_id, tenant_id, recent_tasks, session | 所有 SubTask 复用 |
| **任务级** | task_id, task_intent, user_message, run_id, trace_id | **每个 SubTask 独立分配** |

## 1.3 复用策略

**不提取 `_execute_single()`。** 直接调用现有 `execute(ctx)` 并传入 SubTask 专属的 RuntimeContext。

```
GroupExecutor._execute_sub_task(sub, shared_ctx):
    sub_ctx = _build_sub_context(sub, shared_ctx)
    return await self._ras.execute(sub_ctx)   # ← 现有方法, 零改动
```

---

# 2. TaskGroup 与 TaskDependency 模型

## 2.1 完整模型

```python
class TaskGroup(BaseModel):
    """一组有序 SubTask, 带依赖关系."""
    group_id: str
    sub_tasks: list[SubTaskContext] = []
    dependencies: list[TaskDependency] = []

class TaskDependency(BaseModel):
    """跨 SubTask 引用."""
    from_index: int               # 引用方 (如 Task C)
    to_index: int                 # 被引用方 (如 Task A)
    hint: str                     # "第一篇文章"
    ref_type: str = "ordinal"     # ordinal | temporal | label

    # resolved after `to_index` SubTask executes:
    resolved_task_id: str = ""
    resolved_artifact_type: str = ""  # DRAFT | SCHEDULE
    resolved_resource_id: str = ""    # draft_id | schedule_id

class SubTaskContext(BaseModel):  # Phase 6.0.1
    sub_index: int
    user_message: str
    task_intent: TaskIntent
    task_id: str = ""             # filled after execution
    resolved_resources: Any = None
    result: Any = None
    depends_on_task_index: int | None = None
    depends_on_hint: str = ""
```

## 2.2 依赖解析流程

```
SubTask C: "把第一篇文章发布时间改成晚上9点"
  depends_on_task_index = 0

GroupExecutor 执行到 SubTask C:
  1. SubTask A 已执行完成 → sub_tasks[0].task_id = "task-a"
  2. SubTask A 的 artifacts = [DRAFT(draft-a), SCHEDULE(sched-a)]
  3. 依赖解析:
     dep.resolved_task_id = "task-a"
     dep.resolved_resource_id = "sched-a"   (SCHEDULE type)
  4. 注入 SubTask C 的 RuntimeContext:
     ctx.task_id = "task-a"       ← ResourceResolver 按 task_id 查找
     ctx.task_intent.target_task_id = "task-a"
```

---

# 3. GroupExecutor 设计

## 3.1 职责边界

```
GroupExecutor 职责 (Phase 6.1):

  ✅ 接收 TaskGroup + 共享 RuntimeContext
  ✅ 按序执行 SubTask (被引用的先执行)
  ✅ 为每个 SubTask 构建独立 RuntimeContext
  ✅ 调用现有 execute(ctx) 执行单 Task
  ✅ 解析跨 Task 依赖: 已完成 Task 的 artifacts → 后续 Task 的 target
  ✅ 聚合多个 RuntimeResult
  ✅ 处理部分失败 (继续或停止)

GroupExecutor 不负责:

  ❌ TaskUnderstanding (已由 Decomposer 完成)
  ❌ Plan 生成, Worker 执行 (由 execute() 完成)
  ❌ 跨 Task 的 DAG 编排 (由 Orchestrator 完成单 Task 内编排)
  ❌ 重试逻辑 (由 Worker 完成)
```

## 3.2 核心流程

```python
class GroupExecutor:
    def __init__(self, ras: RuntimeAgentService):
        self._ras = ras

    async def execute(
        self, group: TaskGroup, shared_ctx: RuntimeContext,
    ) -> RuntimeResult:
        results: list[RuntimeResult] = []

        for i, sub in enumerate(group.sub_tasks):
            # 1. 解析跨 Task 依赖
            if sub.depends_on_task_index is not None:
                self._resolve_dependency(sub, group)

            # 2. 构建独立 RuntimeContext
            sub_ctx = self._build_sub_context(sub, shared_ctx)

            # 3. 执行 — 复用现有 execute()
            result = await self._ras.execute(sub_ctx)

            # 4. 记录结果
            sub.task_id = result.task_id
            sub.result = result
            results.append(result)

            # 5. 部分失败策略
            if not result.success and not self._should_continue(result, group, i):
                break

        return self._aggregate(results, group)
```

## 3.3 SubTask RuntimeContext 构建

```python
def _build_sub_context(
    self, sub: SubTaskContext, shared: RuntimeContext,
) -> RuntimeContext:
    return RuntimeContext(
        # 共享设施 — 复用
        llm=shared.llm,
        mcp=shared.mcp,
        auth=shared.auth,
        db_session=shared.db_session,
        timezone=shared.timezone,
        user_id=shared.user_id,
        tenant_id=shared.tenant_id,
        conversation_id=shared.conversation_id,
        session=shared.session,
        recent_tasks=shared.recent_tasks,
        conversation_history=shared.conversation_history,

        # 独立上下文 — 每个 SubTask 新分配
        run_id=str(uuid4()),
        trace_id=str(uuid4()),
        task_id=sub.task_intent.target_task_id or "",  # MODIFY_TASK 时有值
        task_intent=sub.task_intent,
        user_message=sub.user_message,

        # 清理 session 污染 — 每个 SubTask 独立
        active_draft_id=None,
        active_schedule_id=None,
        resolved_resources=None,  # 由 execute() 内部 ResourceResolver 重新填充
    )
```

## 3.4 依赖解析

```python
def _resolve_dependency(
    self, sub: SubTaskContext, group: TaskGroup,
):
    """将跨 Task 引用解析为具体的 target_task_id."""
    dep_idx = sub.depends_on_task_index
    if dep_idx is None:
        return

    dep_sub = group.sub_tasks[dep_idx]
    if not dep_sub.task_id:
        return  # 被引用的 Task 尚未执行 (不应出现, 执行顺序保证)

    # 设置 target_task_id → ResourceResolver 将查找该 Task 的 artifacts
    sub.task_intent.target_task_id = dep_sub.task_id
```

## 3.5 部分失败策略

```python
def _should_continue(
    self, result: RuntimeResult, group: TaskGroup, current_idx: int,
) -> bool:
    """决定是否继续执行后续 SubTask."""

    # 策略 A: 总是继续 (best-effort)
    return True

    # 策略 B: 有下游依赖时停止
    # for sub in group.sub_tasks[current_idx + 1:]:
    #     if sub.depends_on_task_index == current_idx:
    #         return False  # 下游依赖当前 Task → 停止
    # return True
```

**Phase 6.1 默认: 策略 A (best-effort)。**

## 3.6 结果聚合

```python
def _aggregate(
    self, results: list[RuntimeResult], group: TaskGroup,
) -> RuntimeResult:
    """多个 SubTask 结果 → 一个 RuntimeResult."""
    all_ok = all(r.success for r in results)
    status = "COMPLETED" if all_ok else "PARTIAL"

    content_parts = []
    for i, r in enumerate(results):
        tag = "✓" if r.success else "✗"
        content_parts.append(f"{tag} 任务{i+1}: {r.content}")

    return RuntimeResult(
        success=all_ok,
        status=status,
        content="\n\n".join(content_parts),
        execution_path="runtime",
        events=[],
        tool_rounds=sum(r.tool_rounds for r in results),
        artifact_ids=[aid for r in results for aid in (r.artifact_ids or [])],
        draft_id=results[-1].draft_id if results else None,
        partial_results={
            "sub_task_count": len(results),
            "completed_count": sum(1 for r in results if r.success),
        },
    )
```

---

# 4. 接入点

## 4.1 RuntimeAgentService 入口变更

```python
class RuntimeAgentService:
    def __init__(self):
        ...
        self._decomposer = TaskDecomposer()       # Phase 6.0.1
        self._group_executor = GroupExecutor(self)  # Phase 6.1

    async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
        # ── Phase 6.1: Decomposition ──
        tu = TaskUnderstanding(ctx.llm, ctx.model)
        sub_tasks = await self._decomposer.decompose(
            ctx.user_message, tu,
            existing_tasks=...,
        )

        if len(sub_tasks) > 1:
            group = TaskGroup(sub_tasks=sub_tasks)
            return await self._group_executor.execute(group, ctx)

        # ── 单 Task 路径 (不变) ──
        # ... 现有代码 ...

        # 如果 Decomposer 返回了单 SubTask, 用它的 intent 覆盖 ctx
        if sub_tasks:
            ctx.task_intent = sub_tasks[0].task_intent
        # ... 其余不变 ...
```

## 4.2 不变的部分

```
单 Task 路径      — 零改动
CapabilityMapper  — 零改动
Orchestrator      — 零改动
PlanValidator     — 零改动
ExecutionWorker   — 零改动
CapabilityExecutor — 零改动
ToolRuntime       — 零改动
ArtifactStore     — 零改动
ResourceResolver  — 零改动
```

---

# 5. 修改文件

| 操作 | 文件 | 变更 |
|------|------|------|
| **新增** | `task/decomposer.py` | +TaskGroup, +TaskDependency 模型 (Phase 6.0.1 已有 SubTaskContext) |
| **新增** | `services/group_executor.py` | GroupExecutor (~120 行) |
| **修改** | `services/runtime_agent_service.py` | +20 行入口: decompose → group_execute |
| **修改** | `services/runtime_agent_service.py` | execute() 前 12 行: 如果 SubTask 已有 intent, 使用它的 |

---

# 6. 测试案例

| # | 场景 | 期望 |
|---|------|------|
| 1 | 2 个独立 CREATE → GroupExecutor 执行 2 次 | 2 个 RuntimeResult, aggregated status=COMPLETED |
| 2 | 3 个 Task (A→B→C, C 引用 A) | C 执行时 ctx.task_id=A.task_id |
| 3 | Task A 失败 + Task B 无依赖 | B 仍执行 (best-effort) |
| 4 | 单 Task (Decomposer 返回长度 1) | 走现有 execute()，零变化 |
| 5 | Task C 引用 Task A 的 schedule | C 的 ResourceResolver 找到 sched-a |
