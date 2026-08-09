# Phase 6 — Task Decomposition 详细设计

> 日期: 2026-08-07
> 状态: 设计阶段 — 确认后编码

---

# 1. 根因分析：5 个跨 Task 污染点

## 1.1 当前 RuntimeContext（污染源）

```python
# routes.py (line 883-900)
ctx = RuntimeContext(
    task_id=...,            # ← 单值! 多任务只能有一个
    task_intent=...,        # ← 单 TaskIntent! 无法表达 "Task A + Task B + Task C"
    active_draft_id=...,    # ← session 全局! 指向最近创建的 draft
    active_schedule_id=..., # ← session 全局! 指向最近创建的 schedule
    resolved_resources=..., # ← 单次 resolution! 无法区分 Task A 和 Task C 的 target
)
```

## 1.2 污染链路追踪

```
'创建 Spring Boot 文章(A), 创建 Java 集合文章(B), 修改第一篇文章时间(C)'
    │
    ▼ 单TaskIntent
    goal_category = "COMPOSITE"
    requirements = [SEARCH, ANALYZE, CREATE, PUBLISH, CREATE, PUBLISH, UPDATE]
    resource_requests = [CREATE(DRAFT), CREATE(SCHEDULE),  ← Task A
                         CREATE(DRAFT), CREATE(SCHEDULE),  ← Task B
                         UPDATE(SCHEDULE, hint="第一篇文章")]  ← Task C
    │
    ▼ 单 RuntimeContext
    task_id = None  ← NEW_TASK, 没有 target
    │
    ▼ 单 ResourceResolver.resolve()
    ❌ Task C 的 UPDATE(SCHEDULE, "第一篇文章") → TaskResolver 找 "第一篇文章"
    → 找不到! "第一篇文章" 在当前的 "recent_tasks" 中还不存在 (还没创建)
    → 降级为 CREATE(SCHEDULE) → 又多了一个新 schedule!
    │
    ▼ 单 Orchestrator.generate_plan()
    ❌ requirements 包含 SEARCH+ANALYZE+CREATE+PUBLISH+CREATE+PUBLISH+UPDATE
    → has_create=True, has_publish=True → CREATE_AND_PUBLISH (3 steps)
    → 但 Task C 的 UPDATE 被忽略!
    │
    ▼ 单 Worker.run()
    ❌ Step 3: SCHEDULE_PUBLISH → publication.schedule(draft_id=???)
    → 应该用哪个 draft_id? Task A 的? Task B 的?
    → Worker transitively finds the LAST DRAFT artifact (Task B's)
    → 错误! 还有 Task C 的 update 没执行!
```

## 1.3 5 个污染点

| # | 位置 | 问题 | 影响 |
|---|------|------|------|
| P1 | `RuntimeContext.task_id` | 单值 | 多任务共享一个 task_id |
| P2 | `RuntimeContext.task_intent` | 单 TaskIntent | 无法表达多任务 |
| P3 | `RuntimeContext.active_*_id` | session 全局 | 指向最近创建的, 非当前 Task 的 |
| P4 | `RuntimeContext.resolved_resources` | 单次 resolution | Task C 的 cross-ref 未解析 |
| P5 | `RuntimeAgentService.execute()` | 单 Plan + 单 Worker | 所有 requirements 合并到一个 Plan |

---

# 2. 设计方案

## 2.1 核心思路

```
RuntimeContext (共享设施)
  ├── llm, mcp, auth, db_session       ← 所有 SubTask 复用
  ├── conversation_id, user_id, tenant_id
  └── recent_tasks (会话级)

SubTaskContext (每个 Task 独立)          ← NEW
  ├── sub_task_id (独立 task_id)
  ├── user_message (原始片段)
  ├── task_intent (独立理解的结果)
  ├── resolved_resources (独立 resolution)
  │
  ├── plan (独立 Plan)
  ├── execution (独立 Execution)
  └── result (独立 RuntimeResult)
```

## 2.2 数据模型

```python
class SubTaskContext(BaseModel):
    """一个 SubTask 的完整执行上下文."""
    sub_index: int
    user_message: str                         # 原始片段
    task_intent: TaskIntent                   # 独立理解
    task_id: str = ""                         # 执行后分配
    resolved_resources: Any = None            # ResourceResolutionResult
    result: Any = None                        # RuntimeResult

class TaskDependency(BaseModel):
    """跨 SubTask 的 Artifact 依赖."""
    from_index: int                           # 引用方
    to_index: int                             # 被引用方
    hint: str                                 # "第一篇文章"
    ref_type: str = "ordinal"                 # ordinal | temporal | label
    # resolved after `to_index` SubTask executes:
    resolved_task_id: str = ""
    resolved_artifact_type: str = ""          # DRAFT | SCHEDULE
    resolved_resource_id: str = ""            # draft_id | schedule_id

class TaskGroup(BaseModel):
    """一组有序 SubTask."""
    group_id: str
    sub_tasks: list[SubTaskContext] = []
    dependencies: list[TaskDependency] = []
```

## 2.3 SubTaskContext vs RuntimeContext

```
                    RuntimeContext (外层, 不变)
                    ┌─────────────────────────────┐
                    │ llm, mcp, auth, db_session   │
                    │ conversation_id, user_id     │
                    │ recent_tasks                 │
                    │ timezone                     │
                    └──────────────┬──────────────┘
                                   │ 共享
              ┌────────────────────┼────────────────────┐
              │                    │                    │
    SubTaskContext A      SubTaskContext B      SubTaskContext C
    ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
    │intent:       │     │intent:       │     │intent:       │
    │ NEW_TASK     │     │ NEW_TASK     │     │ MODIFY_TASK  │
    │ CREATE+PUB   │     │ CREATE+PUB   │     │ UPDATE       │
    │resolved:     │     │resolved:     │     │resolved:     │
    │ CREATE(DRAFT)│     │ CREATE(DRAFT)│     │ UPDATE(SCHED)│
    │ CREATE(SCHED)│     │ CREATE(SCHED)│     │ → sched-A    │
    │              │     │              │     │              │
    │task_id: tA   │     │task_id: tB   │     │ ref → tA     │
    └──────────────┘     └──────────────┘     └──────────────┘
```

---

# 3. TaskDecomposer

## 3.1 算法

```python
class TaskDecomposer:
    """确定性地拆分为 SubTaskContext 列表。零 LLM 调用。"""

    SPLIT_MARKERS = [
        # 句首 + 分隔词 + 可选前缀
        r"(?:^|。|！|\n)\s*(?:然后|接着|再|最后|另外|同时|此外|并且|还有)\s*(?:帮我|请|再|顺便|麻烦)?\s*",
        # 显式序号
        r"(?:^|。)\s*\d+[.、)）]\s*",
    ]

    ORDINAL = re.compile(r"第\s*([一二三四五六七八九十\d]+)\s*[个篇条项]")

    def decompose(
        self, user_message: str, tu: TaskUnderstanding,
    ) -> list[SubTaskContext]:
        """返回 SubTaskContext 列表。长度=1 表示不需要拆分。"""

        # 1. 拆分
        chunks = self._split(user_message)
        if len(chunks) <= 1:
            return [SubTaskContext(sub_index=0, user_message=user_message,
                                   task_intent=...)]

        # 2. 对每个 chunk 独立理解
        sub_tasks = []
        for i, chunk in enumerate(chunks):
            intent = await tu.understand(chunk)
            if self._is_standalone(intent):
                sub_tasks.append(SubTaskContext(
                    sub_index=i, user_message=chunk, task_intent=intent,
                ))
            else:
                # 非独立 → 合并到前一个
                if sub_tasks:
                    sub_tasks[-1].user_message += " " + chunk

        # 3. 如果合并后只剩 1 个 → 不需要拆分
        if len(sub_tasks) <= 1:
            return sub_tasks

        # 4. 检测跨引用
        for i, st in enumerate(sub_tasks):
            if m := self.ORDINAL.search(st.user_message):
                ordinal = self._parse_ordinal(m.group(1))
                # "第一" → index 0, "第二" → index 1
                if ordinal - 1 < i:  # 引用之前的 Task
                    st.depends_on_task_index = ordinal - 1

        return sub_tasks

    @staticmethod
    def _is_standalone(intent: TaskIntent) -> bool:
        """判断一个 intent 是否能独立构成 Task."""
        return (
            intent.goal_category != "QUERY_INFO"
            and intent.relation != "DIRECT"
            and intent.requirements  # has at least one requirement
        )
```

## 3.2 拆分规则

```
规则 1: 每个 chunk 独立 L1 分析
  → goal_category 不是 QUERY_INFO/DIRECT → 独立
  → 否则 → 不独立, 合并

规则 2: "搜索" 的特殊处理
  → "搜索Java帖子" 单独出现 → 独立 (ANALYZE_COMMUNITY)
  → "搜索Java帖子，然后生成文章" → 两个 chunk:
      chunk 0: 搜索 → 独立? asks_search=True, goal_category=ANALYZE_COMMUNITY
                        → YES, 独立
      chunk 1: 生成文章 → 独立? asks_create or no signal?
      → 如果 chunk 1 不独立, 合并到 chunk 0
      → 最终: 1 个 Task: SEARCH + CREATE

规则 3: 跨引用解析 (执行时)
  → "第一篇文章" → ordinal=1 → SubTaskContext[0]
  → 执行 TaskGroup 时, 先执行被引用的 SubTask
```

---

# 4. GroupExecutor

## 4.1 执行流

```python
class GroupExecutor:
    """按序执行 TaskGroup, 处理依赖解析."""

    def __init__(self, registry, orchestrator, validator):
        ...

    async def execute(
        self, group: TaskGroup, shared_ctx: RuntimeContext,
    ) -> list[RuntimeResult]:
        results = []

        for i, sub in enumerate(group.sub_tasks):
            # 1. 解析跨 Task 依赖
            if sub.depends_on_task_index is not None:
                dep_task = group.sub_tasks[sub.depends_on_task_index]
                self._resolve_dependency(sub, dep_task)

            # 2. 构建独立 RuntimeContext
            sub_ctx = self._build_sub_context(sub, shared_ctx)

            # 3. 执行 — 复用现有单 Task 路径
            result = await self._execute_single_task(sub_ctx)
            results.append(result)
            sub.task_id = result.task_id
            sub.result = result

        return results

    def _resolve_dependency(
        self, sub: SubTaskContext, dep: SubTaskContext,
    ):
        """解析跨 Task 引用.

        Task C: "把第一篇文章发布时间改成晚上9点"
          → dep = SubTaskContext A (已执行, task_id="task-a")
          → intent.target_task_id = "task-a"
          → ResourceResolver.resolve() 时:
              1. task_id="task-a" → 查找 Task A
              2. Task A.artifacts → SCHEDULE(sched-a)
              3. → resource_id = "sched-a"
        """
        if dep.task_id:
            sub.task_intent.target_task_id = dep.task_id
```

## 4.2 SubTaskContext 构建

```python
def _build_sub_context(sub, shared_ctx):
    """从共享 RuntimeContext 构建独立 SubTaskContext."""
    return RuntimeContext(
        # 共享设施 — 复用
        llm=shared_ctx.llm,
        mcp=shared_ctx.mcp,
        auth=shared_ctx.auth,
        db_session=shared_ctx.db_session,
        timezone=shared_ctx.timezone,
        user_id=shared_ctx.user_id,
        conversation_id=shared_ctx.conversation_id,

        # 独立上下文 — 每个 SubTask 独立
        run_id=str(uuid4()),            # 新 run_id
        trace_id=str(uuid4()),          # 新 trace_id
        task_id="",                     # 新 task_id (MODIFY_TASK 除外)
        user_message=sub.user_message,  # 片段文本
        task_intent=sub.task_intent,    # 独立 intent

        # 清理 session 污染
        active_draft_id=None,           # 不继承! 每个 SubTask 独立
        active_schedule_id=None,        # 不继承! 每个 SubTask 独立
    )
```

---

# 5. 集成点

## 5.1 RuntimeAgentService 变更

```python
class RuntimeAgentService:
    def __init__(self):
        self._decomposer = TaskDecomposer()
        self._group_executor = GroupExecutor(...)

    async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
        # ── Phase 6: Decomposition ──
        tu = TaskUnderstanding(ctx.llm, ctx.model)
        sub_tasks = await self._decomposer.decompose(ctx.user_message, tu)

        if len(sub_tasks) > 1:
            group = TaskGroup(sub_tasks=sub_tasks)
            results = await self._group_executor.execute(group, ctx)
            return self._aggregate(results)

        # ── 单 Task 路径 (不变) ──
        ...
```

## 5.2 单 Task 路径零改动

```
len(sub_tasks) == 1:
  → 完全走现有 execute() 路径
  → RuntimeContext, TaskIntent, Orchestrator, Worker 全部不变
```

## 5.3 聚合结果

```python
def _aggregate(self, results: list[RuntimeResult]) -> RuntimeResult:
    """多个 SubTask 的结果聚合为一个 RuntimeResult."""
    all_ok = all(r.success for r in results)
    content_parts = []
    for i, r in enumerate(results):
        content_parts.append(f"{i+1}. {r.content}")
    return RuntimeResult(
        success=all_ok,
        status="COMPLETED" if all_ok else "PARTIAL",
        content="\n\n".join(content_parts),
        ...
    )
```

---

# 6. 修改文件

| 操作 | 文件 | 变更 |
|------|------|------|
| **新增** | `task/decomposer.py` | TaskDecomposer, TaskGroup, SubTaskContext, TaskDependency |
| **修改** | `services/runtime_agent_service.py` | 入口分流: decompose → group_execute / single_execute |
| **修改** | `models/runtime_context.py` | 无需修改 (SubTaskContext 构建时覆盖污染字段) |
| **新增** | `tests/unit/test_task_decomposer.py` | 7 个测试案例 |

### 不修改

```
task/understanding.py     — Decomposer 调用它, 不修改它
task/resolver.py          — 零改动
resource/resolver.py      — 零改动 (跨引用通过 target_task_id 传递)
orchestration/            — 零改动
execution/worker.py       — 零改动
execution/capability_executor.py — 零改动
agent.py                  — 零改动
MCP / Java / Creator      — 零改动
```

# 7. 测试案例

| # | 输入 | chunks | 期望 |
|---|------|--------|------|
| 1 | "创建Java文章然后创建Python文章" | 2 | TaskGroup(2), 都是 NEW_TASK |
| 2 | "搜索Java帖子然后分析原因然后生成文章" | 3→合并为1 | TaskGroup(1), 单Task SEARCH+ANALYZE+CREATE |
| 3 | "创建A明天发布，然后创建B晚上发布，最后把第一个改晚上9点" | 3 | TaskGroup(3), dep(2→0) |
| 4 | "写Spring, 再写Java, 另外搜索Python" | 3 | TaskGroup(3), 两个CREATE+一个SEARCH |
| 5 | "帮我写一篇Spring教程" | 1 | TaskGroup(1), 不变 |
| 6 | "取消Java发布, 再取消Python发布" | 2 | TaskGroup(2), 两个DELETE |
| 7 | 无分隔信号的复合消息 | 1 | TaskGroup(1), 不拆分 |
