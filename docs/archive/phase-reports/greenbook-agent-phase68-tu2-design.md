# Phase 6.8 — TaskUnderstanding 2.0 设计

> 日期: 2026-08-07
> 状态: 设计阶段

---

# 1. 当前 TaskIntent 结构分析

## 1.1 现有字段

```python
class TaskIntent:
    relation: "NEW_TASK" | "MODIFY_TASK" | ...  # 单值, Task 级
    goal_category: "CREATE_CONTENT" | ...         # 单值, 无法表达复合
    goal: str                                      # 一句话摘要
    target_task_id / target_task_hint              # 引用解析
    requirements: [{type: "CREATE"}, ...]          # 有序但扁平
    resource_requests: [{operation, resource_type}] # Phase 5.6
    constraints: [{type, value}]                   # 约束
    confidence: float
    source: "L1" | "L2"
```

## 1.2 结构限制

| 限制 | 影响 |
|------|------|
| `goal_category` 单值 | "运营Java专题" 被归为 IMPROVE_CONTENT 或 CREATE_CONTENT，无法表达 CONTENT_OPERATION |
| `relation` 单值 | "有则修改无则创建" → MODIFY_TASK or NEW_TASK，无法表达 UPSERT |
| `requirements` 扁平 | [SEARCH, ANALYZE, CREATE, PUBLISH] 无顺序约束，无条件分支 |
| 无 `operation_mode` | 无法区分 DIRECT / COMPOSITE / CONDITIONAL |
| 无 `conditions` | "如果...则..." 无法建模 |
| `resource_requests` 从 L1 布尔推导 | CREATE vs UPDATE 决策过早, 应推迟到 ResourceResolver |

# 2. 当前 L1 规则分析

## 2.1 关键词 → 布尔信号 → 单值分类

```
_CREATE_WORDS  → asks_create
_REVISE_WORDS  → asks_revise    if-else 链 → relation + category
_SCHEDULE_WORDS → asks_schedule
...
```

## 2.2 误判场景清单

| # | 输入 | L1 判断 | 正确判断 | 根因 |
|---|------|---------|---------|------|
| 1 | "帮我运营一个Java专题" | MODIFY_TASK, IMPROVE_CONTENT | NEW_TASK, COMPOSITE | "运营" 修复前不在_CREATE_WORDS (6.7 修复) |
| 2 | "有则修改，无则创建" | MODIFY_TASK | CREATE with condition | "修改" 关键词触发, 条件模式未识别 (6.7 部分修复) |
| 3 | "检查已有文章，有则更新" | MODIFY_TASK | QUERY + CONDITIONAL_CREATE | "检查" 无对应 keyword |
| 4 | "对比两篇Java文章" | DIRECT, QUERY_INFO | ANALYZE_COMMUNITY | "对比" 无对应 keyword |
| 5 | "规划下周Java内容发布" | DIRECT, QUERY_INFO | COMPOSITE, CREATE_CONTENT | "规划" 无对应 keyword |
| 6 | "评估当前草稿质量并改进" | MODIFY_TASK | ANALYZE + IMPROVE | "评估" 无对应 keyword |
| 7 | "如果搜索到Spring文章就总结，没有就创建新的" | MODIFY_TASK | CONDITIONAL: SEARCH → IF found: ANALYZE ELSE CREATE | 条件被忽略，只看 "创建" → NEW_TASK |

## 2.3 L1 信号冲突示例

```
消息: "如果有旧文章就修改，否则创建一篇新的"

L1 信号:
  asks_revise = True   (从 "修改")
  asks_create = True   (从 "创建")
  → 同时为 True, 触发 "asks_create and asks_revise" → MODIFY_TASK

修复后:
  _is_conditional() 检测到 "有则" → asks_revise = False
  → asks_create = True → NEW_TASK

但仍丢失: "如果有" 这个条件语义
```

# 3. TaskIntent 2.0 设计

## 3.1 新增字段

```python
class TaskIntent(BaseModel):
    # ── existing fields (保持不变) ──
    relation: TaskRelation = "NEW_TASK"
    goal_category: GoalCategory | str = "QUERY_INFO"
    goal: str = ""
    target_task_id: str | None = None
    target_task_hint: str | None = None
    target_entity_refs: list[EntityHint] = []
    requirements: list[dict] = []
    constraints: list[dict] = []
    resource_requests: list[dict] = []
    confidence: float = 0.0
    source: Literal["L1", "L2"] = "L1"

    # ── NEW Phase 6.8 ──
    operation_mode: Literal["DIRECT", "SIMPLE", "COMPOSITE", "CONDITIONAL"] = "SIMPLE"
    # DIRECT:     "你好" — 不需要 tools
    # SIMPLE:     "写Java文章" — 单目标, 单步骤
    # COMPOSITE:  "搜索+分析+创建+发布" — 多步骤, 一个目标
    # CONDITIONAL: "如果有则修改, 没有则创建" — 带条件分支

    operations: list[dict] = []
    # [{seq: 0, type: "SEARCH", params: {}},
    #  {seq: 1, type: "ANALYZE", params: {}},
    #  {seq: 2, type: "UPSERT", params: {resource: "CONTENT_DRAFT"}},
    #  {seq: 3, type: "APPROVAL", params: {}},
    #  {seq: 4, type: "PUBLISH", params: {}}]

    conditions: list[dict] = []
    # [{seq: 2, type: "IF_EXISTS", resource: "CONTENT_DRAFT",
    #   then: {type: "UPDATE"}, else: {type: "CREATE"}}]
```

## 3.2 operation_mode 决策树

```
                    user_message
                         │
                    _needs_l2_v2()
                    ┌────┴────┐
                    │ NO      │ YES
                    ▼         ▼
              L1 快速路径    L2 深度路径
              mode=SIMPLE    mode=COMPOSITE/CONDITIONAL
              (现有逻辑)     (LLM Structured Output)
```

## 3.3 增强 _needs_l2_v2()

```python
@staticmethod
def _needs_l2_v2(text: str) -> bool:
    """Phase 6.8: broader L2 trigger for complex scenarios."""
    # 1. Existing triggers (keep)
    if any(w in text for w in _AMBIGUOUS_VERBS):
        return True
    count = sum(text.count(m) for m in _COMPOSITE_MARKERS)
    if count >= 2:
        return True
    for pat in _CROSS_REF_PATTERNS:
        if pat.search(text):
            return True

    # 2. NEW: length-based heuristic
    if len(text) > 100:
        return True

    # 3. NEW: conditional patterns
    if re.search(r"有则|无则|如果.{0,15}(?:有|没有|存在|不存在|找到)", text):
        return True

    # 4. NEW: operations keywords
    if any(w in text for w in ("运营", "规划", "策划", "评估", "对比", "检查")):
        return True

    # 5. NEW: multi-line / numbered list
    if re.search(r"\d+[.、)]\s", text):
        return True

    return False
```

## 3.4 L2 Prompt 增强

```python
_L2_SYSTEM_V2 = """Analyze the user message for GreenBook community operations.
Return valid JSON only.

## Output schema
{
  "operation_mode": "SIMPLE" | "COMPOSITE" | "CONDITIONAL",
  "relation": "NEW_TASK" | "MODIFY_TASK" | ...,
  "goal_category": "CREATE_CONTENT" | "IMPROVE_CONTENT" | ...,
  "goal": "one-sentence summary",
  "operations": [
    {"seq": 0, "type": "SEARCH", "description": "..."},
    {"seq": 1, "type": "ANALYZE", "description": "..."},
    {"seq": 2, "type": "CREATE" | "UPDATE" | "UPSERT", "resource": "CONTENT_DRAFT"},
    {"seq": 3, "type": "APPROVAL"},
    {"seq": 4, "type": "PUBLISH"}
  ],
  "conditions": [
    {"seq": 2, "type": "IF_EXISTS", "resource": "CONTENT_DRAFT",
     "then": {"type": "UPDATE"}, "else": {"type": "CREATE"}}
  ],
  "constraints": [
    {"type": "TIME", "value": "..."},
    {"type": "APPROVAL", "value": "before_publish"}
  ],
  "target_task_hint": "...",
  "confidence": 0.8
}

## Rules
- "运营/规划/策划 + 搜索/分析/创建" → COMPOSITE
- "有则...无则.../如果...就...否则..." → CONDITIONAL
- "发布前确认/让我审一下" → APPOVAL constraint
- Numbered steps (1. 2. 3.) → COMPOSITE with seq ordering
- "刚才/上次/之前" → target_task_hint

## Operation types
SEARCH, ANALYZE, CREATE, UPDATE, DELETE, UPSERT, APPOVAL, PUBLISH, QUERY

## Examples
Input: "帮我运营Java专题：搜索热门，分析原因，检查已有文章，有则修改无则创建，发布前确认，确认后发布"
Output:
{
  "operation_mode": "CONDITIONAL",
  "relation": "NEW_TASK",
  "goal_category": "CREATE_CONTENT",
  "goal": "运营Java并发专题",
  "operations": [
    {"seq":0,"type":"SEARCH","description":"搜索热门Java帖子"},
    {"seq":1,"type":"ANALYZE","description":"分析热门原因"},
    {"seq":2,"type":"UPSERT","resource":"CONTENT_DRAFT"},
    {"seq":3,"type":"APPROVAL"},
    {"seq":4,"type":"PUBLISH"}
  ],
  "conditions": [
    {"seq":2,"type":"IF_EXISTS","resource":"CONTENT_DRAFT",
     "then":{"type":"UPDATE"},"else":{"type":"CREATE"}}
  ],
  "constraints": [
    {"type":"APPROVAL","value":"before_publish"}
  ]
}"""
```

# 4. 修改文件列表

| 操作 | 文件 | 变更 |
|------|------|------|
| **修改** | `task/models.py` | TaskIntent +`operation_mode`, +`operations`, +`conditions` |
| **修改** | `task/understanding.py` | `_needs_l2()` → `_needs_l2_v2()`; L2 prompt 增强; L2 解析 operations/conditions |
| **修改** | `orchestration/orchestrator.py` | `_select_template()` 增加 `operations` 输入 (优先于 requirements) |
| **修改** | `services/runtime_agent_service.py` | `_execute_single()` 增加 conditional 处理 |
| **新增** | `tests/unit/test_tu_v2.py` | TU 2.0 专项测试 (10+ cases) |

### 不修改

```
Orchestrator 模板     — 零改动 (FULL_PIPELINE 等模板不变)
Worker               — 零改动
ToolRuntime          — 零改动
ResourceResolver     — 零改动
GroupExecutor        — 零改动
HITL                 — 零改动
agent.py             — 零改动
```

# 5. 风险评估

| 风险 | 等级 | 缓解 |
|------|------|------|
| TaskIntent 新增字段破坏旧代码 | 🟢 低 | 所有新增字段有默认值, 旧代码只读 `requirements` 不变 |
| L2 调用频率增加 (成本) | 🟡 中 | `_needs_l2_v2()` 放宽了触发条件, 但 < 30% 的请求会触发 L2 |
| L2 JSON 输出不稳定 | 🟡 中 | Pydantic 校验 + JSON repair + L1 fallback |
| Orchestrator 需要适配 `operations` | 🟢 低 | `operations` 和 `requirements` 都可映射到模板选择 |
| 542 测试回归 | 🟢 低 | TaskIntent 新字段只增不改, L1 逻辑最小改动 |

# 6. 实施步骤

| Step | 内容 | 时间 |
|------|------|------|
| 1 | `task/models.py`: TaskIntent +3 字段 | 30min |
| 2 | `task/understanding.py`: `_needs_l2_v2()` + L2 prompt v2 | 1h |
| 3 | `task/understanding.py`: L1/L2 填充 operation_mode + operations + conditions | 1h |
| 4 | `orchestration/orchestrator.py`: 增加 operations→template 映射 | 30min |
| 5 | `services/runtime_agent_service.py`: conditional 处理 | 30min |
| 6 | 测试: 10+ TU 2.0 cases + 542 回归 | 1h |
