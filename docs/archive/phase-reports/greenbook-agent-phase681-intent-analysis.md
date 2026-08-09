# Phase 6.8.1 — Intent Analysis 层设计

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 6 个失败 Case 根因分析

| # | 输入 | L1 信号 | 问题 |
|---|------|---------|------|
| 1 | "创建一个Python教程" | 全部 False | "创建" 不在 `_CREATE_WORDS`，需要 "创建一篇" |
| 2 | "查看我的草稿" | create=True | "草稿" 在 `_CREATE_WORDS` 中！应属于 QUERY |
| 3 | "把发布时间改成晚上9点" | revise=True | "改" 触发 revise → IMPROVE_CONTENT，应 SCHEDULE+UPDATE |
| 4 | "如果有旧文章则优化" | revise=True | "优化" 触发 revise，条件语义丢失 |
| 5 | "搜索...然后创建文章" | search=True | "创建" 单独不匹配，"然后" 未触发复合检测 |
| 6 | "发布之前让我确认" | 全部 False | "发布" 无时间 → 不触发 schedule，"确认" 无匹配 |

## 1.1 根因分类

| 类别 | 案例 | 根因 |
|------|------|------|
| **关键词缺失** | 1, 5 | "创建"、"发布"、"搜索" 的变体不在 keyword 列表中 |
| **关键词误触** | 2 | "草稿" 同时出现在 _CREATE_WORDS 和 context 中，歧义 |
| **信号冲突** | 4 | "优化" vs "创建" 同时出现，L1 if-else 链只有一个能胜出 |
| **无对应 keyword** | 6 | "确认"、"审核" 不是任何 keyword，整句无匹配 |
| **资源/动作混淆** | 3 | "改" 触发 revise(内容修改)，但实际是要修改 schedule(时间修改) |

# 2. Intent Analysis 层设计

## 2.1 核心思路: Action × Resource × Condition

```
用户消息
    │
    ▼
IntentAnalyzer (新增)
    │
    ├── 解析 Action:  CREATE | UPDATE | QUERY | PUBLISH | SEARCH | CANCEL
    ├── 解析 Resource: CONTENT | DRAFT | SCHEDULE | POST | TASK
    ├── 解析 Condition:  NONE | IF_EXISTS | IF_NOT_EXISTS
    │
    ▼
IntentSpec (新增, TaskIntent 的超集)
    │
    ▼
TaskIntent (兼容, 从 IntentSpec 推导)
```

## 2.2 IntentSpec 模型

```python
class ActionType(StrEnum):
    CREATE = "CREATE"       # 创建新资源
    UPDATE = "UPDATE"       # 修改已有资源
    DELETE = "DELETE"       # 删除资源
    QUERY = "QUERY"         # 查询资源
    PUBLISH = "PUBLISH"     # 发布
    SEARCH = "SEARCH"       # 搜索
    ANALYZE = "ANALYZE"     # 分析
    APPROVE = "APPROVE"     # 审批

class ResourceType(StrEnum):
    CONTENT = "CONTENT"           # 文章/帖子内容
    DRAFT = "DRAFT"               # 草稿
    SCHEDULE = "SCHEDULE"         # 定时发布
    POST = "POST"                 # 已发布帖子
    TASK = "TASK"                 # 任务(抽象)

class ConditionType(StrEnum):
    NONE = "NONE"
    IF_EXISTS = "IF_EXISTS"           # 如果资源存在
    IF_NOT_EXISTS = "IF_NOT_EXISTS"   # 如果资源不存在
    BEFORE_PUBLISH = "BEFORE_PUBLISH" # 发布前

class IntentSpec(BaseModel):
    """Phase 6.8.1: Action + Resource + Condition 分解."""

    # 操作列表 (有序)
    actions: list[dict] = []
    # [{seq: 0, action: SEARCH, resource: POST, params: {topic: "Java"}},
    #  {seq: 1, action: ANALYZE, resource: null},
    #  {seq: 2, action: CREATE, resource: CONTENT, condition: IF_NOT_EXISTS}]

    # 全局约束
    constraints: list[dict] = []
    # [{type: TIME, value: "明天9点"}, {type: APPROVAL, value: BEFORE_PUBLISH}]

    # 引用
    target_hint: str | None = None

    # 整体模式
    mode: str = "SIMPLE"  # SIMPLE | COMPOSITE | CONDITIONAL

    # 置信度
    confidence: float = 0.0
    source: str = "L1"
```

## 2.3 L1 重构: 从 if-else 到 Action 映射

### 当前: 关键词 → 布尔信号 → if-else 链 → 单 category

```python
# 当前逻辑
if asks_cancel:
    relation = "CANCEL_TASK"
elif asks_create and asks_revise:
    relation = "MODIFY_TASK"
elif asks_revise:
    relation = "MODIFY_TASK"
...
```

### 新: 关键词 → Action + Resource → IntentSpec

```python
# 新逻辑: 每个 keyword 映射到 (action, resource) 对
ACTION_MAP = {
    # CREATE actions
    "写一篇":     (ActionType.CREATE, ResourceType.CONTENT),
    "创建一篇":   (ActionType.CREATE, ResourceType.CONTENT),
    "生成一篇":   (ActionType.CREATE, ResourceType.CONTENT),
    "运营":       (ActionType.CREATE, ResourceType.CONTENT),
    "创建":       (ActionType.CREATE, ResourceType.CONTENT),  # ← NEW: 宽松匹配

    # UPDATE actions (指定 resource 类型)
    "修改":       (ActionType.UPDATE, ResourceType.DRAFT),
    "改成":       (ActionType.UPDATE, ResourceType.DRAFT),
    "优化":       (ActionType.UPDATE, ResourceType.DRAFT),
    "完善":       (ActionType.UPDATE, ResourceType.DRAFT),

    # QUERY actions
    "查看":       (ActionType.QUERY, ResourceType.DRAFT),
    "列出":       (ActionType.QUERY, ResourceType.DRAFT),
    "看一下":     (ActionType.QUERY, ResourceType.TASK),

    # PUBLISH actions
    "发布":       (ActionType.PUBLISH, ResourceType.CONTENT),
    "定时":       (ActionType.PUBLISH, ResourceType.SCHEDULE),

    # SEARCH actions
    "搜索":       (ActionType.SEARCH, ResourceType.POST),
    "查找":       (ActionType.SEARCH, ResourceType.POST),
    "找一下":     (ActionType.SEARCH, ResourceType.POST),
}

# Resource context disambiguation
# "草稿" → CONTEXT key. If action=VIEW → QUERY(DRAFT), not CREATE(DRAFT)
# "发布时间" → SCHEDULE context. "改发布时间" → UPDATE(SCHEDULE), not UPDATE(CONTENT)

CONTEXT_HINTS = {
    "草稿":   ResourceType.DRAFT,
    "发布时间": ResourceType.SCHEDULE,
    "定时":   ResourceType.SCHEDULE,
    "发布":   ResourceType.CONTENT,  # unless combined with "定时"
}
```

## 2.4 消歧规则

```python
def _resolve_actions(actions: list[tuple]) -> IntentSpec:
    """
    消歧规则:

    1. "草稿" 消歧:
       如果 action 附近有 "查看/列出" → QUERY(DRAFT)
       如果 action 附近有 "修改/优化" → UPDATE(DRAFT)
       如果没有 action keyword → default QUERY(DRAFT)

    2. "发布时间" 消歧:
       "改发布时间" → UPDATE(SCHEDULE), not UPDATE(CONTENT)
       "发布时间改成..." → UPDATE(SCHEDULE)

    3. 条件检测:
       "有则...无则..." → condition=IF_EXISTS
       "如果有...就..." → condition=IF_EXISTS
       "没有则" → condition=IF_NOT_EXISTS

    4. 动作排序:
       SEARCH → ANALYZE → CREATE/UPDATE → PUBLISH
       条件动作以 condition 标记，不改变排序
    """
```

## 2.5 IntentSpec → TaskIntent 兼容推导

```python
def to_task_intent(spec: IntentSpec) -> TaskIntent:
    """从 IntentSpec 推导旧 TaskIntent (向后兼容)."""

    # 从 actions 推导 requirements
    reqs = []
    for a in spec.actions:
        if a["action"] in ("SEARCH",):
            reqs.append({"type": "SEARCH"})
        elif a["action"] in ("ANALYZE",):
            reqs.append({"type": "ANALYZE"})
        elif a["action"] in ("CREATE",):
            reqs.append({"type": "CREATE"})
        elif a["action"] in ("UPDATE",):
            reqs.append({"type": "IMPROVE"})
        elif a["action"] in ("PUBLISH",):
            reqs.append({"type": "PUBLISH"})

    # 从第一个 action 推导 goal_category
    primary = spec.actions[0]["action"] if spec.actions else "QUERY"
    category_map = {
        "CREATE": "CREATE_CONTENT",
        "UPDATE": "IMPROVE_CONTENT",
        "SEARCH": "ANALYZE_COMMUNITY",
        "PUBLISH": "PUBLISH_CONTENT",
        "QUERY": "QUERY_INFO",
        "DELETE": "MANAGE_SCHEDULE",
    }

    return TaskIntent(
        relation="NEW_TASK" if primary in ("CREATE", "SEARCH") else "MODIFY_TASK",
        goal_category=category_map.get(primary, "QUERY_INFO"),
        requirements=reqs,
        resource_requests=_derive_resource_requests(spec),
        ...
    )
```

# 3. 修复后的预期结果

| 输入 | 当前 | 新 IntentSpec |
|------|------|-------------|
| "创建一个Python教程" | DIRECT, QUERY_INFO | actions=[CREATE(CONTENT)], mode=SIMPLE |
| "查看我的草稿" | NEW_TASK, CREATE_CONTENT | actions=[QUERY(DRAFT)], mode=SIMPLE |
| "把发布时间改成晚上9点" | MODIFY_TASK, IMPROVE_CONTENT | actions=[UPDATE(SCHEDULE)], mode=SIMPLE |
| "如果有旧文章则优化" | MODIFY_TASK, IMPROVE_CONTENT | actions=[UPDATE(CONTENT, IF_EXISTS)], mode=CONDITIONAL |
| "搜索热门内容然后创建文章" | NEW_TASK, ANALYZE_COMMUNITY | actions=[SEARCH(POST), CREATE(CONTENT)], mode=COMPOSITE |
| "发布之前让我确认" | DIRECT, QUERY_INFO | actions=[PUBLISH(CONTENT)], constraints=[APPROVAL] |

# 4. 修改文件

| 操作 | 文件 | 变更 |
|------|------|------|
| **新增** | `task/intent_analyzer.py` | IntentAnalyzer, IntentSpec, ActionType, ConditionType |
| **修改** | `task/models.py` | TaskIntent +`intent_spec` (可选字段) |
| **修改** | `task/understanding.py` | L1: `_quick_intent()` → 调用 IntentAnalyzer; 保留旧逻辑为 fallback |

### 不修改

```
task/understanding.py L2  — 不变
Orchestrator              — 不变
Worker                    — 不变
RuntimeAgentService       — 不变
548 测试                   — 全部保持通过
```

# 5. 风险

| 风险 | 缓解 |
|------|------|
| IntentAnalyzer 输出与旧 TaskIntent 不一致 | to_task_intent() 保守推导; 旧 L1 逻辑保留为 fallback |
| "创建" 宽松匹配导致误判 | 结合 context hints 消歧 |
| 新增模块复杂度 | IntentAnalyzer 是纯函数, ~80 行 |

# 6. 预期基线提升

| 类别 | 当前 | 预期 |
|------|------|------|
| SIMPLE | 67% | 83% |
| MODIFY | 80% | 100% |
| COMPOSITE | 80% | 100% |
| CONDITIONAL | 60% | 80% |
| HITL | 33% | 67% |
| **OVERALL** | **67%** | **83%** |
