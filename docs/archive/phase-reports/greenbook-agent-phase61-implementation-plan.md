# Phase 6.1 — GroupExecutor 实施方案

> 日期: 2026-08-07
> 前置: Phase 6.0.1 TaskDecomposer (完成)
> 状态: 设计确认中

---

# 1. execute() 拆分分析

## 1.1 当前结构

```
RuntimeAgentService.execute(ctx)  (~160 行)
  ├── [1]  task_id = ctx.task_id or uuid4()
  ├── [2]  Capabilities check          ← ctx.task_intent
  ├── [3]  Resource Resolution         ← ctx.task_intent.resource_requests
  ├── [4]  Plan generation             ← ctx.task_intent.requirements
  ├── [5]  Validation
  ├── [6]  Observability setup         ← ctx.trace_id, ctx.run_id
  ├── [7]  ToolRuntime setup           ← ctx.mcp
  ├── [8]  CapabilityExecutor setup
  ├── [9]  Worker init + run
  ├── [10] Artifact collection
  └── [11] Result build
```

## 1.2 拆分方案

```
execute(ctx)
  │
  ├── TaskDecomposer.decompose(ctx.user_message)
  │     │
  │     ├── len==1 → 使用 SubTask 的 intent 更新 ctx.task_intent
  │     │             → return self._execute_single(ctx)
  │     │
  │     └── len>1  → GroupExecutor.execute(group, ctx)
  │                   └── 对每个 SubTask: self._execute_single(sub_ctx)
  │
  └── (旧代码变为 _execute_single, 逻辑不变)

_execute_single(ctx) → RuntimeResult
  │
  ├── 步骤 [1]~[11], 与当前 execute() 完全一致
  │
  └── 关键: ctx 已经包含独立 task_intent + task_id, 不依赖外部状态
```

## 1.3 递归预防

```
execute()
  └── _execute_single()          ← 不调 execute()

GroupExecutor.execute()
  └── _execute_single(sub_ctx)   ← 不调 execute(), 直接调 _execute_single

调用图:
  execute → decomposer → [len>1] → GroupExecutor → _execute_single
  execute → decomposer → [len=1] → _execute_single
  GroupExecutor 永远不会调 execute() ← 无递归
```

---

# 2. 最终模型设计

## 2.1 SubTaskContext (Phase 6.0.1 扩展)

```python
class SubTaskContext(BaseModel):
    sub_index: int
    user_message: str
    task_intent: TaskIntent        # 不可变 — 理解层输出
    task_id: str = ""
    resolved_resources: Any = None  # ResourceResolutionResult (Phase 5.6)
    result: Any = None              # RuntimeResult (执行后填充)

    # cross-task dependency
    depends_on_task_index: int | None = None
    depends_on_hint: str = ""       # "第一篇文章"

    # NEW Phase 6.1: resolved dependency resources
    dependency_resources: dict[str, str] = {}
    # {"draft_id": "draft-a", "schedule_id": "sched-a"}
```

## 2.2 TaskDependency

```python
class TaskDependency(BaseModel):
    """dependent_task depends on source_task."""
    dependent_task_index: int     # 引用方 (Task C)
    source_task_index: int        # 被引用方 (Task A)
    hint: str = ""                # "第一篇文章"
    ref_type: str = "ordinal"

    # resolved after source_task executes:
    resolved_draft_id: str = ""
    resolved_schedule_id: str = ""
```

## 2.3 TaskGroup

```python
class TaskGroup(BaseModel):
    group_id: str
    sub_tasks: list[SubTaskContext] = []
    dependencies: list[TaskDependency] = []
```

---

# 3. GroupExecutor 设计

## 3.1 放置位置

```
apps/assistant_api/greenbook_assistant_api/services/group_executor.py
```

**理由:** GroupExecutor 调用 RuntimeAgentService._execute_single()，需要访问 RAS 实例。放在同一 package 下无循环导入。

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
            # 1. 检查上游依赖是否失败
            if self._should_skip(sub, group, results):
                results.append(self._skip_result(sub, group, results))
                continue

            # 2. 解析依赖资源 (不修改 TaskIntent)
            dep_resources = self._resolve_dep_resources(sub, group)
            sub.dependency_resources = dep_resources

            # 3. 构建独立 RuntimeContext
            sub_ctx = self._build_sub_context(sub, shared_ctx)

            # 4. 执行
            result = await self._ras._execute_single(sub_ctx)
            sub.task_id = result.task_id
            sub.result = result
            results.append(result)

        return self._aggregate(results, group)
```

## 3.3 依赖资源解析 (不修改 TaskIntent)

```python
def _resolve_dep_resources(
    self, sub: SubTaskContext, group: TaskGroup,
) -> dict[str, str]:
    """从已执行的 source Task 的 artifacts 解析依赖资源."""
    if sub.depends_on_task_index is None:
        return {}

    source = group.sub_tasks[sub.depends_on_task_index]
    if not source.task_id or not source.result:
        return {}

    resources: dict[str, str] = {}

    # 设置 target_task_id — 这个需要放到 ctx 上 (不是 TaskIntent)
    # ctx.task_id = source.task_id  ← 在 _build_sub_context 中设置

    # 从 source.result 中提取 artifact IDs
    if source.result.draft_id:
        resources["draft_id"] = source.result.draft_id
    if source.result.schedule_id:
        resources["schedule_id"] = source.result.schedule_id

    return resources
```

## 3.4 SubTask RuntimeContext 构建

```python
def _build_sub_context(
    self, sub: SubTaskContext, shared: RuntimeContext,
) -> RuntimeContext:
    # 依赖资源 → 注入 ctx.task_id (供 ResourceResolver 使用)
    task_id = ""
    if sub.depends_on_task_index is not None:
        source = sub_tasks[sub.depends_on_task_index]
        task_id = source.task_id or ""

    return RuntimeContext(
        # 共享 — 复用
        llm=shared.llm, mcp=shared.mcp, auth=shared.auth,
        db_session=shared.db_session, timezone=shared.timezone,
        user_id=shared.user_id, tenant_id=shared.tenant_id,
        conversation_id=shared.conversation_id,
        session=shared.session,
        recent_tasks=shared.recent_tasks,
        conversation_history=shared.conversation_history,

        # 独立 — 每个 SubTask 新分配
        run_id=str(uuid4()),
        trace_id=str(uuid4()),
        task_id=task_id,                     # ← 依赖解析结果
        task_intent=sub.task_intent,         # ← 不修改!
        user_message=sub.user_message,

        # 清理污染
        active_draft_id=None,
        active_schedule_id=None,
        resolved_resources=None,
    )
```

## 3.5 部分失败策略

```python
def _should_skip(
    self, sub: SubTaskContext, group: TaskGroup, results: list[RuntimeResult],
) -> bool:
    """如果上游依赖失败 → 跳过当前 SubTask."""
    if sub.depends_on_task_index is None:
        return False

    source_result = results[sub.depends_on_task_index]
    return not source_result.success

def _skip_result(self, sub, group, results):
    return RuntimeResult(
        success=False, status="SKIPPED",
        content=f"任务{sub.sub_index+1}已跳过：依赖的任务{sub.depends_on_task_index+1}执行失败",
        execution_path="runtime",
    )
```

```
示例:
  Task0 失败, Task1 独立, Task2 依赖 Task0
  → Task0: FAILED
  → Task1: 执行 (无依赖) ✓
  → Task2: SKIPPED (上游 Task0 失败)
```

---

# 4. RuntimeResult 兼容性

## 4.1 单 Task (不变)

```python
RuntimeResult(
    success=True/False,
    status="COMPLETED"/"FAILED",
    content="已完成：...",
    draft_id="draft-a",
    ...
)
```

## 4.2 Group Task (新增)

```python
RuntimeResult(
    success=all_ok,
    status="COMPLETED" if all_ok else "PARTIAL",
    content="✓ 任务1: 已完成...\n\n✓ 任务2: 已完成...\n\n✗ 任务3: 已跳过...",
    execution_path="runtime",
    partial_results={
        "group": True,
        "sub_task_count": 3,
        "completed_count": 2,
        "failed_count": 1,
    },
)
```

**前端兼容性:** `status` 新增 `"PARTIAL"` 和 `"SKIPPED"` 值。前端需要适配。

---

# 5. Trace 设计

## 5.1 新增事件类型

```python
class EventType(StrEnum):
    # ... existing events ...

    # Phase 6.1 — Group-level events
    GROUP_CREATED = "GROUP_CREATED"
    SUB_TASK_STARTED = "SUB_TASK_STARTED"
    SUB_TASK_COMPLETED = "SUB_TASK_COMPLETED"
    SUB_TASK_SKIPPED = "SUB_TASK_SKIPPED"
    GROUP_COMPLETED = "GROUP_COMPLETED"
```

## 5.2 事件发射时机

```
GroupExecutor.execute():
  → GROUP_CREATED (group_id, sub_task_count)
  → for each sub:
      → SUB_TASK_STARTED (sub_index, user_message)
      → [execute _execute_single — 产生单 Task trace events]
      → SUB_TASK_COMPLETED (sub_index, status)
      or SUB_TASK_SKIPPED
  → GROUP_COMPLETED (status, completed_count, failed_count)
```

## 5.3 Trace 存储

不修改 TraceCollector。GROUP 事件复用现有 TraceEvent，通过 event_type 区分。

---

# 6. 修改文件列表

| 操作 | 文件 | 变更 |
|------|------|------|
| **修改** | `task/decomposer.py` | +TaskGroup, +TaskDependency; SubTaskContext +dependency_resources |
| **新增** | `services/group_executor.py` | GroupExecutor (~120 行) |
| **修改** | `services/runtime_agent_service.py` | execute() → decomposer + 分流; 旧代码 → _execute_single() |
| **新增** | `tests/unit/test_group_executor.py` | 5 个测试 |
| **修改** | `observability/models.py` | EventType +GROUP_CREATED/SUB_TASK_*/GROUP_COMPLETED |

### 不修改

```
task/understanding.py     — 零改动
task/resolver.py          — 零改动
resource/resolver.py      — 零改动
orchestration/            — 零改动
execution/worker.py       — 零改动
execution/capability_executor.py — 零改动
agent.py                  — 零改动
LegacyAgentService        — 零改动
MCP/Java/Creator          — 零改动
```

---

# 7. 风险分析

| 风险 | 等级 | 缓解 |
|------|------|------|
| execute() → _execute_single() 重构引入 bug | 🟡 中 | _execute_single 是 execute() 的精确复制; 单 Task 路径逻辑完全不变; 391 测试回归 |
| GroupExecutor 调用 _execute_single 产生递归 | 🟢 低 | GroupExecutor 不调 execute(), 直接调 _execute_single(); execute() 只被 routes.py 调用 |
| SubTaskContext 构建遗漏字段 | 🟡 中 | 字段对照 execute() 的 ctx 使用逐项检查; 测试覆盖 |
| TaskIntent 被修改 | 🟢 低 | SubTaskContext.task_intent 是 deep copy; _execute_single 只读不写 |
| RuntimeResult.status 新增值前端不兼容 | 🟢 低 | "PARTIAL"/"SKIPPED" 是新增值, 旧值 "COMPLETED"/"FAILED" 不变 |
| 部分失败依赖链判定错误 | 🟡 中 | 测试覆盖: Task0 失败 → Task2 跳过, Task1 继续 |

---

# 8. 测试案例

| # | 场景 | 期望 |
|---|------|------|
| 1 | 2 个独立 CREATE → GroupExecutor | status=COMPLETED, 2 results |
| 2 | Task0 失败 + Task1 独立 | Task1 继续执行, status=PARTIAL |
| 3 | Task0 失败 + Task2 依赖 Task0 | Task2 SKIPPED |
| 4 | 单 Task (Decomposer len=1) | 走 _execute_single, 行为不变 |
| 5 | 3 Tasks with cross-ref | Task2 的 ctx.task_id = Task0.task_id |

---

# 9. 实施步骤

| Step | 内容 | 时间 |
|------|------|------|
| 1 | execute() → _execute_single() 提取 + execute() 分流入口 | 30min |
| 2 | SubTaskContext +dependency_resources | 10min |
| 3 | TaskGroup + TaskDependency 模型 | 10min |
| 4 | GroupExecutor | 1h |
| 5 | EventType 新增 | 5min |
| 6 | 测试 (5 cases) | 30min |
| 7 | 391 回归 | 10min |
