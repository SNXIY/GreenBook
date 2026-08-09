# GreenBook Agent Runtime — Task Decomposition Layer 设计

> 日期: 2026-08-07
> 触发: 真实交互暴露的单轮复杂请求无法拆分为多个独立 Task 的问题
> 状态: 设计阶段 — 确认后编码

---

# 1. 问题分析

## 1.1 真实案例

```
用户:
"创建 Spring Boot 虚拟线程文章，明天10点发布。
然后再创建 Java 集合文章，晚上8点发布。
最后把第一篇文章发布时间改成晚上9点。"
```

**期望拆分为 3 个独立 Task:**

```
Task A: CREATE_CONTENT (Spring Boot 虚拟线程) + SCHEDULE (明天 10:00)
Task B: CREATE_CONTENT (Java 集合) + SCHEDULE (晚上 8:00)
Task C: UPDATE_SCHEDULE (target=Task A, 晚上 9:00)
         ↑ 跨 Task 引用
```

**当前系统行为:** 整个消息被当作一个 TaskIntent 处理，search→create→schedule→search→create→schedule→update 全部塞入一个 Plan。

## 1.2 当前架构能力检查

### TaskUnderstanding

| 能力 | 状态 | 说明 |
|------|------|------|
| 检测复合信号 | ⚠️ 部分 | `_needs_l2()` 检测 "然后" 出现次数，但只触发 L2，不拆分 |
| 拆分多个 Task | ❌ 不支持 | `TaskIntent` 是单任务模型，无 `sub_tasks` 概念 |
| 识别跨 Task 引用 | ❌ 不支持 | "第一篇文章"、"之前那个" 无法解析为另一个 Task |

### TaskIntent

```python
class TaskIntent:
    relation: "NEW_TASK"           # 单值，无法表达 "这是一个 Task 组"
    goal_category: "CREATE_CONTENT" # 单值
    requirements: [{type: CREATE}]  # 单任务需求
    resource_requests: [...]        # 单任务资源请求
    # 无 sub_tasks, 无 task_group, 无 cross_references
```

### Orchestrator

```python
def generate_plan(task_id, goal_category, requirements):
    # 单 Plan 生成
    # 无 "为 TaskGroup 生成多个 Plan" 的能力
```

### ResourceResolver

```python
def resolve(requests, tasks):
    # 按 hint 在当前会话 Tasks 中查找
    # "第一篇文章" → 需要 TaskGroup 上下文才能解析
```

# 2. Task Decomposition Layer 设计

## 2.1 核心概念

```
用户消息
  │
  ▼
TaskDecomposer                     ← NEW
  │
  ├── 检测分隔信号: "然后"、"再"、"同时"、"另外"、"最后"
  ├── 按分隔点拆分为多个片段
  ├── 每个片段独立调用 TaskUnderstanding → TaskIntent
  ├── 识别片段间的引用: "第一篇文章"、"之前那个"、"刚才那个"
  │
  ▼
TaskGroup                          ← NEW
  ├── tasks: list[SubTask]
  │     ├── SubTask(index=0, intent=TaskIntent, user_message="创建...")
  │     ├── SubTask(index=1, intent=TaskIntent, user_message="再创建...")
  │     └── SubTask(index=2, intent=TaskIntent, user_message="最后把第一篇文章...")
  │
  └── dependencies: list[TaskDependency]
        └── TaskDependency(from=2, to=0, hint="第一篇文章")
```

## 2.2 数据模型

```python
class SubTask(BaseModel):
    """TaskGroup 中的一个子任务."""
    index: int                           # 0-based position in user message
    user_message: str                    # 原始片段文本
    intent: TaskIntent                   # 独立理解的意图
    task_id: str = ""                    # 执行后分配的 Task ID


class TaskDependency(BaseModel):
    """跨 SubTask 引用关系."""
    from_index: int                      # 引用方 (如 Task C 的 index)
    to_index: int                        # 被引用方 (如 Task A 的 index)
    hint: str                            # "第一篇文章"、"之前那个"
    resolved_task_id: str = ""           # 执行后解析


class TaskGroup(BaseModel):
    """一组有序的 SubTask."""
    group_id: str
    sub_tasks: list[SubTask] = []
    dependencies: list[TaskDependency] = []
    execution_order: list[int] = []      # [0, 1, 2] or [0, 2, 1] if 2 depends on 0


class DecomposerResult(BaseModel):
    """TaskDecomposer 输出."""
    is_composite: bool = False           # True when message was split
    task_group: TaskGroup | None = None
    single_intent: TaskIntent | None = None  # when is_composite=False
```

## 2.3 TaskDecomposer 设计

```python
class TaskDecomposer:
    """将复合消息拆分为 TaskGroup."""

    # 分隔信号 (有序, 越靠前优先级越高)
    SPLIT_MARKERS = [
        # 完整分隔短语 (带顺序语义)
        (r"(?:然后|接着|之后|完了|再|最后|另外|同时|并且)\s*(?:帮我|请|再)?\s*", True),
        # 弱分隔
        (r"(?:还有一个|另外还有|此外|另外还要)", True),
    ]

    # 跨 Task 引用模式
    CROSS_REF_PATTERNS = [
        (r"第\s*([一二三四五六七八九十\d]+)\s*[个篇条项]", "ordinal"),  # "第一个"、"第二篇"
        (r"(?:之前|刚才|上面|前面)(?:那个|那篇|那个任务)", "temporal"),
        (r"把\s*(?:前面|上面|刚才|之前)\s*(?:那个|那篇)", "temporal"),
    ]

    def decompose(
        self,
        user_message: str,
        tu: TaskUnderstanding,
        existing_tasks: list[Task],
    ) -> DecomposerResult:
        """
        1. 检测分隔信号
        2. 按分隔点拆分消息
        3. 每个片段独立调用 TaskUnderstanding
        4. 检测跨片段引用
        5. 返回 DecomposerResult
        """
```

### 分隔逻辑

```
输入: "创建 Spring Boot 文章，明天10点发布。
       然后再创建 Java 集合文章，晚上8点发布。
       最后把第一篇文章发布时间改成晚上9点。"

Step 1: 按 SPLIT_MARKERS 拆分
  → 片段 0: "创建 Spring Boot 文章，明天10点发布。"
  → 片段 1: "创建 Java 集合文章，晚上8点发布。"
  → 片段 2: "把第一篇文章发布时间改成晚上9点。"

Step 2: 每个片段独立理解
  → SubTask 0: TaskIntent(NEW_TASK, CREATE_CONTENT, [CREATE, PUBLISH])
  → SubTask 1: TaskIntent(NEW_TASK, CREATE_CONTENT, [CREATE, PUBLISH])
  → SubTask 2: TaskIntent(MODIFY_TASK, MANAGE_SCHEDULE, [UPDATE])

Step 3: 跨片段引用检测
  → 片段 2 包含 "第一篇文章" → ordinal=1 → matches SubTask 0
  → TaskDependency(from=2, to=0, hint="第一篇文章")

Step 4: 确定执行顺序
  → Task C depends on Task A → A 必须先执行
  → execution_order = [0, 1, 2]  (A first, then B and C in parallel)
```

## 2.4 与现有模块集成

### RuntimeAgentService 流程变更

```
当前:
  TaskIntent → ResourceResolver → Orchestrator → Worker

新:
  UserMessage
    ↓
  TaskDecomposer.decompose()
    ├── is_composite=False → 当前路径 (不变)
    └── is_composite=True → TaskGroup 路径:
        │
        ├── For each SubTask in execution_order:
        │     ├── ResourceResolver.resolve(sub_task.intent, tasks)
        │     │     └── 跨 Task 引用: 使用已完成 SubTask 的 task_id 解析 hint
        │     ├── Orchestrator.generate_plan()
        │     └── Worker.run()
        │
        └── 返回聚合 RuntimeResult
```

### ResourceResolver 集成

```python
# 当 SubTask 2 引用 "第一篇文章":
# 1. 查找 TaskDependency: from=2, to=0
# 2. SubTask 0 已经执行完毕, task_id 已分配
# 3. 将 task_id 注入 SubTask 2 的 intent.target_task_id
# 4. ResourceResolver 按 task_id 查找 Task.artifacts → schedule_id
```

---

# 3. 关键设计决策

## 3.1 Decomposer 放在哪里？

**方案 A: 在 TaskUnderstanding 之前**
→ TaskDecomposer 先拆分消息, 然后每个片段独立调用 TaskUnderstanding
✅ 简单, 不影响现有 TaskUnderstanding 逻辑
✅ 每个片段独立理解, 互不干扰

**方案 B: 在 TaskUnderstanding 内部**
→ L2 LLM 输出包含 sub_tasks 字段
❌ LLM 不稳定, 拆分逻辑应该确定性
❌ 增加 L2 prompt 复杂度

**选择: 方案 A**

## 3.2 跨 Task 引用如何解析？

```
"第一篇文章" → ordinal=1 → SubTask[0].task_id
                 ↑ 执行后才分配

策略:
  1. 排序 execution_order: 被引用的 Task 先执行
  2. 执行完 SubTask 0 后, task_id 已知
  3. 执行 SubTask 2 时, 将 task_id 注入 intent.target_task_id
  4. ResourceResolver 正常解析
```

## 3.3 是否需要 LLM？

**分隔检测:** 不需要 LLM — 纯正则匹配分隔信号
**每个片段理解:** 复用现有 TaskUnderstanding (L1+L2)
**跨片段引用:** 不需要 LLM — 纯正则匹配序数/时间引用

**整个 Decomposer 是确定性的, 零 LLM 调用。**

---

# 4. 实现计划

## 4.1 新增模块

```
packages/assistant_core/greenbook_assistant_core/task/
    decomposer.py       # TaskDecomposer + TaskGroup + SubTask + TaskDependency
```

## 4.2 修改文件

| 文件 | 变更 |
|------|------|
| `task/decomposer.py` | **新增** — TaskDecomposer, TaskGroup, SubTask, TaskDependency, DecomposerResult |
| `task/models.py` | 可选: TaskIntent 增加 `is_sub_task` 标记 |
| `services/runtime_agent_service.py` | 集成 TaskDecomposer: 入口分流 composite vs single |
| `tests/unit/test_task_decomposer.py` | **新增** — 5+ 测试 |

## 4.3 测试案例

```python
# Case 1: 简单拆分 (无跨引用)
"创建Java文章然后创建Python文章"
→ TaskGroup(size=2), is_composite=True

# Case 2: 跨引用 (序数)
"创建Java文章明天发布，然后创建Python文章晚上发布，最后把第一篇文章改晚上9点"
→ TaskGroup(size=3), TaskDependency(from=2, to=0)

# Case 3: 单一任务 (无拆分)
"帮我写一篇Java文章"
→ is_composite=False, single_intent 有效

# Case 4: "再" as separator
"写一篇Java文章，再写一篇Python文章"
→ TaskGroup(size=2)

# Case 5: "另外" as separator
"创建Spring文章，另外创建Java文章"
→ TaskGroup(size=2)

# Case 6: 复合消息但无分隔信号
"搜索Java帖子并分析然后生成文章" (全部关于同一个 Task)
→ is_composite=False (没有跨 Task 分隔)
```

## 4.4 风险

| 风险 | 缓解 |
|------|------|
| "然后" 既是 Task 分隔也是单 Task 内步骤信号 | 按句号/换行 + "然后" 组合判断。同句内的 "然后" 不分隔 |
| Decomposer 拆分错误导致无用 Task | 拆分后每个片段都经过 TaskUnderstanding 验证, 无效片段返回 DIRECT |
| 跨 Task 引用解析失败 | 引用解析失败的 SubTask 独立执行 (降级: 不带 target), 不阻塞其他 SubTask |

---

# 5. 执行顺序

| Phase | 内容 |
|-------|------|
| 6.0a | `task/decomposer.py` — TaskDecomposer 实现 |
| 6.0b | 集成到 RuntimeAgentService |
| 6.0c | 测试 (6 cases + 现有 377 回归) |
