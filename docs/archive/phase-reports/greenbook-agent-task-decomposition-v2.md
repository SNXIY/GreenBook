# Task Decomposition Layer — 设计 v2

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 核心问题：单步还是多任务？

## 1.1 两种"然后"

```
类型 A — 单任务多步骤:
  "搜索Java帖子，然后分析原因，然后生成文章"
  → 1 个 Task: ANALYZE_COMMUNITY (search → analyze → create)
    拆开后片段: "搜索Java帖子" ✓ 独立, "分析原因" ✗ 不独立, "生成文章" ✗ 不独立

类型 B — 多独立任务:
  "创建Java文章明天发布，然后创建Python文章晚上发布"
  → 2 个 Task: Task A(CREATE+PUBLISH), Task B(CREATE+PUBLISH)
    拆开后片段: "创建Java文章明天发布" ✓ 独立, "创建Python文章晚上发布" ✓ 独立
```

## 1.2 区分规则

**规则: 拆分后每个片段能否独立构成一个有效的 TaskIntent。**

```
Decomposer 流程:

Step 1: 按分隔信号拆分消息
  → chunks

Step 2: 每个 chunk 做 L1 快速分析
  → 判断是否是 "独立片段"

Step 3: 独立片段 → SubTask
        非独立片段 → 合并到前一个 SubTask 或丢弃

Step 4: 检测跨片段引用
```

### "独立片段" 判断标准

一个 chunk 是独立的，当它在 L1 中命中以下任一条件:

| L1 信号 | 示例 | 是否独立 |
|---------|------|---------|
| `asks_create=True` | "创建Java文章明天发布" | ✅ 独立 — 有独立的"创建"意图 |
| `asks_revise=True` + hint | "修改Java文章标题" | ✅ 独立 — 有独立的"修改"意图 |
| `asks_cancel=True` | "取消定时发布" | ✅ 独立 |
| `asks_schedule=True` + NO asks_create + NO asks_revise | "把发布时间改晚上9点" | ⚠️ 需要 hint 指向已有 Task → 独立但有依赖 |
| `asks_search=True` + NO asks_create | "搜索Java帖子" | ⚠️ 搜索通常是前置步骤, 不是独立任务 |
| 无任何信号 | "分析原因"、"生成文章" | ❌ 不独立 — 缺少主体 |

### "搜索" 的特殊处理

```
"搜索Java帖子并分析原因然后生成文章"
  → chunks: ["搜索Java帖子", "分析原因", "生成文章"]
  → L1: asks_search ✓, 但后续 "生成文章" 没有独立主体
  → 合并为 1 个 Task: SEARCH + ANALYZE + CREATE

"搜索Java帖子" (单独出现，无后续 "生成/写")
  → 独立 Task: ANALYZE_COMMUNITY
```

**规则: 搜索 + 后续 CREATE 信号 → 同一个 Task。搜索单独出现 → 独立 Task。**

---

# 2. 数据模型

## 2.1 SubTask

```python
class SubTask(BaseModel):
    """TaskGroup 中的一个子任务."""
    index: int                           # 0-based position
    user_message: str                    # 原始片段文本
    intent: TaskIntent                   # 独立理解的结果
    task_id: str = ""                    # 执行后分配的 Task ID
    status: str = "PENDING"              # PENDING | RUNNING | COMPLETED | FAILED
```

## 2.2 TaskDependency

```python
class TaskDependency(BaseModel):
    """跨 SubTask 引用关系."""
    from_index: int                      # 引用方
    to_index: int                        # 被引用方
    hint: str                            # 原始引用文本: "第一篇文章"
    ref_type: str = ""                   # "ordinal" | "temporal" | "label"
    resolved_task_id: str = ""           # 执行 to_index 后填充
    resolved_resource_id: str = ""       # 执行 to_index 后从 artifacts 解析
```

## 2.3 TaskGroup

```python
class TaskGroup(BaseModel):
    """一组有序 SubTask."""
    group_id: str
    conversation_id: str
    sub_tasks: list[SubTask] = []
    dependencies: list[TaskDependency] = []
```

## 2.4 GroupExecution

```python
class GroupExecution(BaseModel):
    """TaskGroup 的执行状态."""
    group_id: str
    status: str = "PENDING"              # PENDING | RUNNING | COMPLETED | PARTIAL
    sub_results: list[RuntimeResult] = []
    current_index: int = 0               # 当前执行到的 SubTask index

    @property
    def next_sub_task(self) -> SubTask | None: ...
```

---

# 3. TaskDecomposer 设计

## 3.1 核心算法

```python
class TaskDecomposer:
    """将复合消息拆分为 TaskGroup。零 LLM 调用。"""

    # 分隔信号
    SPLIT_PATTERN = re.compile(
        r"(?:^|[。！\n])\s*"              # 句首或句号/换行之后
        r"(?:然后|接着|再|最后|另外|同时|此外|并且|还有)\s*"
        r"(?:帮我|请|再|顺便|麻烦)?\s*"
    )

    # 跨 Task 引用
    ORDINAL_REF = re.compile(
        r"第\s*([一二三四五六七八九十\d]+)\s*[个篇条项]"
    )

    def decompose(
        self,
        user_message: str,
        tu: TaskUnderstanding,
    ) -> TaskGroup | None:
        """返回 None 表示不需要拆分 (单任务)."""

        # 1. 按分隔信号拆分
        chunks = self._split(user_message)

        # 2. 单 chunk → 不需要拆分
        if len(chunks) <= 1:
            return None

        # 3. L1 分析每个 chunk
        analyzed = [self._analyze_chunk(c, tu) for c in chunks]

        # 4. 合并非独立片段
        merged = self._merge_dependent(analyzed)

        # 5. 只剩 1 个 → 不需要拆分
        if len(merged) <= 1:
            return None

        # 6. 检测跨引用
        deps = self._detect_cross_refs(merged)

        # 7. 构建 TaskGroup
        return TaskGroup(
            sub_tasks=[
                SubTask(index=i, user_message=m["text"], intent=m["intent"])
                for i, m in enumerate(merged)
            ],
            dependencies=deps,
        )
```

## 3.2 拆分逻辑

```
输入: "创建 Spring Boot 文章明天发布。
       然后再创建 Java 集合文章晚上发布。
       最后把第一篇文章发布时间改成晚上9点。"

Step 1: SPLIT_PATTERN 匹配
  → 找到 2 个分隔点: "然后再"、"最后把"
  → chunks = [
      "创建 Spring Boot 文章明天发布。",
      "创建 Java 集合文章晚上发布。",
      "把第一篇文章发布时间改成晚上9点。"
    ]

Step 2: L1 分析
  → chunk 0: asks_create=True, asks_schedule=True → 独立 ✓
  → chunk 1: asks_create=True, asks_schedule=True → 独立 ✓
  → chunk 2: asks_schedule=True, "改" → asks_revise → 独立 ✓

Step 3: 合并 (无需要合并的)

Step 4: 跨引用
  → chunk 2: "第一篇文章" → ordinal=1 → SubTask[0]
```

## 3.3 区分逻辑: 合并非独立片段

```
输入: "搜索Java帖子，然后分析原因，然后生成文章"

Step 1: SPLIT_PATTERN 匹配
  → "然后分析原因"、"然后生成文章"
  → chunks = ["搜索Java帖子，", "分析原因，", "生成文章"]

Step 2: L1 分析
  → chunk 0: asks_search=True → "搜索" 独立? 看后续
  → chunk 1: no signal → 不独立 ✗
  → chunk 2: no signal (没有 "写/创建") → 不独立 ✗

Step 3: 合并
  → chunk 1 不独立 → 合并回 chunk 0
  → chunk 2 不独立 → 合并回 chunk 0
  → merged = [完整消息] → len=1 → 不拆分 ✓
```

---

# 4. 集成方式

## 4.1 RuntimeAgentService 入口变更

```python
class RuntimeAgentService:
    def __init__(self):
        ...
        self._decomposer = TaskDecomposer()

    async def execute(self, ctx: RuntimeContext) -> RuntimeResult:
        # ── 0. Decomposition ──
        tu = TaskUnderstanding(ctx.llm, ctx.model)
        group = self._decomposer.decompose(ctx.user_message, tu)

        if group is not None:
            return await self._execute_group(group, ctx)

        # ── existing single-task path ──
        ...
```

## 4.2 GroupExecution 流

```python
async def _execute_group(self, group: TaskGroup, ctx: RuntimeContext):
    execution = GroupExecution(group_id=group.group_id)

    for sub in self._execution_order(group):
        # 1. 解析跨 Task 依赖
        if deps := self._deps_for(sub.index, group):
            self._resolve_deps(deps, execution, ctx)

        # 2. 执行 SubTask
        sub_ctx = self._build_sub_context(sub, ctx)
        result = await self._execute_single_task(sub_ctx)  # 现有路径
        execution.sub_results.append(result)
        sub.task_id = result.task_id
        sub.status = "COMPLETED" if result.success else "FAILED"

    return self._aggregate_results(execution)
```

## 4.3 依赖解析

```
Task C: "把第一篇文章发布时间改成晚上9点"
  → TaskDependency(from=2, to=0, hint="第一篇文章")

GroupExecution 执行到 SubTask 2 时:
  1. SubTask 0 已执行完成, task_id = "task-a"
  2. 查找 task-a 的 artifacts → schedule-a
  3. 注入 SubTask 2 的 RuntimeContext:
     ctx.task_id = None  (这是独立的新 Run, 不关联到 task-a)
     ctx.resolved_resources = ResourceTarget(
       operation=UPDATE, resource_type=SCHEDULE,
       resource_id="schedule-a", task_id="task-a"
     )
  4. Orchestrator → SINGLE_MANAGE_SCHEDULE → publication.update_schedule(schedule_id="schedule-a")
```

---

# 5. 不修改的模块

```
agent.py                    — 零改动
LegacyAgentService          — 零改动
MCP 全部                    — 零改动
Java / Creator              — 零改动
TaskUnderstanding           — 零改动 (Decomposer 调用它, 不修改它)
TaskResolver                — 零改动
ResourceResolver            — 零改动
Orchestrator                — 零改动
ExecutionWorker             — 零改动
```

---

# 6. 实现计划

## 新增

```
packages/assistant_core/greenbook_assistant_core/task/decomposer.py   (~200 行)
tests/unit/test_task_decomposer.py                                   (~150 行)
```

## 修改

```
services/runtime_agent_service.py   (+30 行: 入口分流)
```

## 测试案例

| # | 输入 | 期望 |
|---|------|------|
| 1 | "创建Java文章然后创建Python文章" | TaskGroup(2), 都是 NEW_TASK+CREATE |
| 2 | "搜索Java帖子然后分析原因然后生成文章" | is_composite=False (不拆分, 1个Task) |
| 3 | "创建A明天发布，然后创建B晚上发布，最后把第一个改晚上9点" | TaskGroup(3), dep(2→0) |
| 4 | "写一篇Java文章，再写一篇Python文章" | TaskGroup(2) |
| 5 | "帮我写一篇Spring教程" | is_composite=False |
| 6 | "取消Java文章发布，再取消Python文章发布" | TaskGroup(2), 两个 DELETE SCHEDULE |
| 7 | "创建Java文章，另外搜索社区Python热门帖子" | TaskGroup(2), CREATE+SEARCH |
