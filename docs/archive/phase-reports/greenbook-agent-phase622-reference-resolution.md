# Phase 6.2.2 — Task Reference Resolution 设计

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 当前能力分析

## 1.1 TaskResolver 已覆盖

| 引用类型 | 示例 | 机制 | 状态 |
|---------|------|------|------|
| 显式 ID | task_id="task-a" | L1 exact_id | ✅ |
| 标签匹配 | "Java文章" | L2 substring/token | ✅ |
| Artifact 匹配 | "搜索结果" | L3 artifact summary | ✅ |
| 类别匹配 | IMPROVE_CONTENT → 最近同类别 | L4 category | ✅ |
| 最近回退 | (无 hint) | L5 recency | ✅ |
| 纯时间引用 | "刚才那个" | temporal → L4/L5 | ⚠️ 低精度 |
| 序数引用 | "第一篇" | Decomposer ORDINAL_REF | ⚠️ 仅限 TaskGroup 内 |

## 1.2 未覆盖

| 引用类型 | 示例 | 当前行为 | 问题 |
|---------|------|---------|------|
| 时间+内容 | "昨天那个文章" | temporal-only → L4/L5 | 不按时间过滤 |
| 相对时间 | "刚才创建的" | temporal-only → L4/L5 | 不区分 "刚才" vs "上次" |
| 操作+时间 | "上一次发布任务" | temporal-only | 不按 goal_category 过滤 |
| 跨 conversation | "上周那个文章" | 无跨会话查询 | TaskResolver 只看当前 conversation |

---

# 2. 设计方案

## 2.1 定位

```
TaskReferenceResolver (新增)
  │
  ├── 解析自然语言引用 → ReferenceHint
  │     { time: "昨天", category: "CREATE_CONTENT", keyword: "文章" }
  │
  ├── 按优先级查询:
  │     1. TaskGroup 内依赖引用 (已有, Decomposer)
  │     2. 当前 conversation 内 Task (增强 TaskResolver)
  │     3. 跨 conversation 查询 (预留, Phase 7)
  │
  └── 返回: ReferenceResolution { targets[], is_ambiguous, needs_clarification }
```

## 2.2 数据模型

```python
class ReferenceHint(BaseModel):
    """自然语言引用的结构化分解."""
    raw: str                            # 原始文本
    time_ref: str = ""                  # "昨天" | "刚才" | "上周" | "上一次" | ""
    ordinal: int | None = None          # 1, 2, 3 (第X篇)
    keyword: str = ""                   # "文章" | "帖子" | "发布"
    category_hint: str = ""             # derived from keyword → goal_category
    is_temporal_only: bool = False      # pure "刚才那个", no content hints

class ReferenceResolution(BaseModel):
    """一次引用解析的完整结果."""
    hint: ReferenceHint
    targets: list[ResolvedTaskTarget] = []
    best_match: ResolvedTaskTarget | None = None
    is_ambiguous: bool = False
    needs_clarification: bool = False
    resolution_path: str = ""           # "task_group" | "conversation" | "history"
```

## 2.3 核心算法

```python
class TaskReferenceResolver:
    """解析自然语言引用 → 历史 Task 候选."""

    # 时间表达式 → created_at 过滤范围
    TIME_PATTERNS = {
        "刚才":   (0, 300),         # < 5 分钟
        "刚刚":   (0, 300),
        "今天":   (0, 86400),       # < 24 小时
        "昨天":   (86400, 172800),  # 24-48 小时前
        "前天":   (172800, 259200),
        "上周":   (604800, 1209600),
        "上一次": (0, 999999999),   # 不限时间, 取第2个
        "之前":   (0, 999999999),   # 不限时间, 排除最近1个
    }

    def resolve(
        self,
        hint_text: str,
        tasks: list[Task],              # ordered by recency
        *,
        group_context: TaskGroup | None = None,  # Phase 6.1
    ) -> ReferenceResolution:
        """
        1. 解析 hint → ReferenceHint
        2. TaskGroup 内引用 (ordinal "第一篇")
        3. 按时间过滤 tasks
        4. 按 keyword 匹配
        5. 按 category 匹配
        6. 检测歧义
        """

    def _parse_hint(self, text: str) -> ReferenceHint:
        """'昨天那个Java文章' → {time:'昨天', keyword:'Java文章', category:'CREATE_CONTENT'}."""

    def _filter_by_time(self, tasks: list[Task], time_ref: str) -> list[Task]:
        """按时间窗口过滤 tasks."""
        window = self.TIME_PATTERNS.get(time_ref)
        if window is None:
            return tasks
        now = datetime.now(UTC)
        return [t for t in tasks
                if window[0] <= (now - t.created_at).total_seconds() <= window[1]]

    def _resolve_in_group(self, ordinal: int, group: TaskGroup) -> ResolvedTaskTarget | None:
        """TaskGroup 内: '第一篇文章' → SubTask[0]."""
```

## 2.4 示例走查

### "昨天那个文章"

```
1. _parse_hint("昨天那个文章")
   → ReferenceHint(time="昨天", keyword="文章")

2. _filter_by_time(tasks, "昨天")
   → 24-48小时前创建的 tasks

3. _match_by_keyword(filtered_tasks, "文章")
   → 内容类 Task (CREATE_CONTENT or IMPROVE_CONTENT)

4. 结果:
   1 个匹配 → best_match=task-xyz
   0 个匹配 → fallback: 不限时间, keyword 匹配
   多个匹配 → is_ambiguous=True
```

### "刚才创建的帖子"

```
1. _parse_hint("刚才创建的帖子")
   → ReferenceHint(time="刚才", keyword="创建", category="CREATE_CONTENT")

2. _filter_by_time(tasks, "刚才")
   → < 5 分钟前创建的 tasks (通常就是最近 1-2 个)

3. _match_by_category(filtered_tasks, "CREATE_CONTENT")
   → 类别精确匹配

4. 结果:
   1 个 → best_match
   0 个 → 扩大时间窗口 → 今天的 CREATE_CONTENT tasks
```

### "刚才那个" (歧义)

```
1. _parse_hint("刚才那个")
   → ReferenceHint(time="刚才", is_temporal_only=True)

2. _filter_by_time(tasks, "刚才")
   → < 5 分钟前

3. 无 keyword, 无 category → 所有匹配

4. 结果:
   0 个 → 扩大窗口
   1 个 → best_match (confidence=0.30)
   多个 → is_ambiguous=True ← NEW! 同时间窗口多个 Task
```

## 2.5 歧义检测规则

```python
def _detect_ambiguity(self, targets: list[Task], hint: ReferenceHint) -> bool:
    """以下情况触发歧义:"""
    # 1. 纯时间引用 + 同窗口 >= 2 个 Task
    if hint.is_temporal_only and len(targets) >= 2:
        return True

    # 2. keyword 匹配到 >= 2 个 Task
    if hint.keyword and len(targets) >= 2:
        return True

    # 3. 时间过滤后 0 个 → 扩大窗口后 >= 2 个
    # (暂不触发歧义, 返回 None 让外层处理)

    return False
```

---

# 3. 与现有模块的关系

```
TaskReferenceResolver     ← Phase 6.2.2 NEW
  │
  ├── 调用 TaskResolver.resolve()  ← L2-L5 匹配 (不改)
  ├── 读取 Task.created_at         ← 时间过滤 (新增)
  ├── 读取 TaskGroup context       ← ordinal 引用 (Phase 6.1)
  └── 返回 ReferenceResolution     ← unified result

不修改:
  TaskResolver      — 零改动
  ResourceResolver  — 零改动
  GroupExecutor     — 零改动
  Worker            — 零改动
  ToolRuntime       — 零改动
```

---

# 4. 实现计划

## 4.1 新增文件

```
packages/assistant_core/greenbook_assistant_core/task/reference_resolver.py
    ReferenceHint (model)
    ReferenceResolution (model)
    TaskReferenceResolver (class)
```

## 4.2 修改文件

```
无 — 纯新增模块, 不修改任何现有文件
```

## 4.3 测试

| # | 输入 | 任务列表 | 期望 |
|---|------|---------|------|
| 1 | "昨天那个文章" | task-a(昨天, CREATE_CONTENT), task-b(今天, CREATE_CONTENT) | target=task-a |
| 2 | "第一篇文章" | TaskGroup([task-a, task-b]) | target=task-a (group context) |
| 3 | "刚才那个" | task-a(刚才, CREATE), task-b(刚才, IMPROVE) | is_ambiguous=True |
| 4 | "上周的Java文章" | task-a(上周, CREATE, "Java"), task-b(上周, SEARCH) | target=task-a |
| 5 | "创建Java文章" (无时间) | task-a, task-b(Java) | fallback to TaskResolver |

---

# 5. 代码量估算

- `reference_resolver.py`: ~180 行
- `tests/unit/test_reference_resolver.py`: ~120 行
- 不修改任何现有文件
- 预计 399 → 405 passed
