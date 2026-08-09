# Phase 6.8.1 — TaskUnderstanding 2.0 最终设计方案

> 日期: 2026-08-07
> 状态: 设计阶段 — 确认后编码 Stage A

---

# 1. 当前可复用代码分析

## 1.1 直接复用

| 代码 | 位置 | 复用方式 |
|------|------|---------|
| `TaskUnderstanding.__init__(llm, model)` | understanding.py:71 | 不变 — LLM 注入接口 |
| `TaskUnderstanding.understand()` | understanding.py:77 | 入口不变 — 内部增加 IntentSpec 路径 |
| `_llm_understand()` | understanding.py:214 | 复用 LLM 调用模式, prompt 替换 |
| `_parse_llm_output()` | understanding.py:251 | 复用 JSON parse + Pydantic 校验模式 |
| `_needs_l2()` | understanding.py:288 | 增强为 `_needs_l2_v2()` — 结构信号评分 |
| `_has_future_time()` | understanding.py:304 | 不变 |
| `_is_conditional()` | understanding.py:313 | 不变 |
| `_asks_for_community()` | understanding.py:319 | 不变 |
| `TaskIntent` 所有现有字段 | models.py:51 | 不变 — 新增字段有默认值 |

## 1.2 不修改的模块

```
orchestration/orchestrator.py  — 零改动
execution/worker.py            — 零改动
execution/capability_executor.py — 零改动
runtime/tool_runtime.py        — 零改动
resource/resolver.py           — 零改动
services/runtime_agent_service.py — 零改动 (Stage A)
services/group_executor.py     — 零改动
human/manager.py               — 零改动
agent.py                       — 零改动
```

---

# 2. IntentSpec 最终模型

## 2.1 原则

**IntentSpec 只描述用户意图，不生成执行计划。**

禁止字段: `seq`, `depends_on`, `step_id`, `parallelizable`, `output_artifact_type`, `input_artifact_types`, `template_name`

这些属于 Planner 职责。

## 2.2 完整模型

```python
# ── new file: task/intent_models.py ──

class IntentMode(StrEnum):
    SIMPLE = "SIMPLE"           # single action, no conditions
    COMPOSITE = "COMPOSITE"     # multiple actions, single goal
    CONDITIONAL = "CONDITIONAL" # actions with conditions

class ActionType(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    QUERY = "QUERY"
    SEARCH = "SEARCH"
    ANALYZE = "ANALYZE"
    PUBLISH = "PUBLISH"

class ResourceType(StrEnum):
    CONTENT = "CONTENT"       # 文章/帖子正文
    DRAFT = "DRAFT"           # 草稿
    SCHEDULE = "SCHEDULE"     # 定时发布
    POST = "POST"             # 已发布帖子
    TASK = "TASK"             # 抽象任务

class ConditionType(StrEnum):
    IF_EXISTS = "IF_EXISTS"
    IF_NOT_EXISTS = "IF_NOT_EXISTS"

class ConstraintType(StrEnum):
    TIME = "TIME"
    APPROVAL = "APPROVAL"
    USER_INPUT = "USER_INPUT"

# ── core types ──

class IntentAction(BaseModel):
    """用户想做的一个动作."""
    action: ActionType
    resource: ResourceType | None = None      # 操作的资源类型
    confidence: float = 0.0

class IntentCondition(BaseModel):
    """条件分支."""
    type: ConditionType
    resource: ResourceType | None = None      # 条件检查的资源
    then_action: ActionType | None = None     # 条件为真时的动作
    else_action: ActionType | None = None     # 条件为假时的动作

class IntentConstraint(BaseModel):
    """执行约束."""
    type: ConstraintType
    value: str = ""                           # "明天9点", "BEFORE_PUBLISH"

class IntentSpec(BaseModel):
    """用户意图的完整结构化表示.

    不包含任何执行计划信息 (无 seq, depends_on, step_id).
    这些由 Orchestrator 从 actions + conditions 推导.
    """

    mode: IntentMode = IntentMode.SIMPLE
    goal: str = ""                            # 一句话摘要

    actions: list[IntentAction] = []          # 操作列表 (无顺序约束)
    conditions: list[IntentCondition] = []    # 条件分支
    constraints: list[IntentConstraint] = []  # 全局约束

    target_hint: str | None = None            # 引用提示
    confidence: float = 0.0
    source: str = "L1"

# ── validator result ──

class IntentValidationResult(BaseModel):
    """IntentValidator 的输出."""
    is_valid: bool = True
    needs_repair: bool = False
    warnings: list[str] = []
    errors: list[str] = []
    suggested_fixes: list[str] = []
```

## 2.3 与 TaskIntent 的关系

```
IntentSpec                              TaskIntent
─────────                               ─────────
mode                                    (无直接映射, 用于 routing)
goal                                    goal
actions[0].action                       relation (NEW_TASK vs MODIFY_TASK)
actions → requirement types             requirements
actions → resource types                resource_requests
constraints                             constraints
conditions                              (暂不映射, Phase 7)
target_hint                             target_task_hint
confidence                              confidence
source                                  source

转换函数: IntentSpec → TaskIntent  (to_task_intent)
```

---

# 3. L1 / L2 路由方案

## 3.1 路由流程

```
understand(user_message)
    │
    ├── [L1] _quick_intent() → TaskIntent (现有, 不改)
    │      用于: 简单明确的单步请求
    │      输出: source="L1"
    │
    ├── [_needs_l2_v2()] 结构信号评分
    │      score >= 阈值 → 进入 L2
    │      score <  阈值 → 返回 L1 结果
    │
    ├── [L2] _llm_understand_v2() → IntentSpec
    │      LLM Structured Output (JSON) → Pydantic IntentSpec
    │
    ├── [Validator] IntentValidator.validate(spec, original_text)
    │      is_valid → 使用 L2 结果
    │      needs_repair → 重试一次 L2
    │      仍失败 → fallback L1
    │
    └── to_task_intent(spec) → TaskIntent (兼容下游)
```

## 3.2 _needs_l2_v2() — 结构信号评分

```python
@staticmethod
def _needs_l2_v2(text: str) -> bool:
    """结构信号评分 ≥ 2 → 触发 L2."""
    score = 0

    # 条件表达 (高权重)
    if re.search(r"如果|否则|有则|无则|要是|假如", text):
        score += 3

    # 多步骤信号 (中权重)
    count = sum(text.count(m) for m in ("然后", "再", "最后", "同时", "并且"))
    if count >= 1:
        score += 1
    if count >= 2:
        score += 2

    # 编号列表 (高权重)
    if re.search(r"\d+[.、)]\s", text):
        score += 3

    # 开放目标词 (中权重)
    if any(w in text for w in ("运营", "规划", "策划", "专题")):
        score += 2

    # 长消息 (低权重)
    if len(text) > 100:
        score += 1

    # 模糊动词 (低权重, 但至少触发)
    if any(w in text for w in ("优化", "完善", "打磨", "提升", "调整")):
        score += 1

    return score >= 2
```

# 4. LLM Structured Output Schema

## 4.1 Prompt

```python
_L2_SYSTEM_V2 = """You are an intent understanding module for a community operations assistant.

Analyze the user message and output a structured JSON object.

## Output Schema (strict)
{
  "mode": "SIMPLE" | "COMPOSITE" | "CONDITIONAL",
  "goal": "one-sentence summary of what the user wants",
  "actions": [
    {"action": "CREATE"|"UPDATE"|"DELETE"|"QUERY"|"SEARCH"|"ANALYZE"|"PUBLISH",
     "resource": "CONTENT"|"DRAFT"|"SCHEDULE"|"POST"|"TASK"|null}
  ],
  "conditions": [
    {"type": "IF_EXISTS"|"IF_NOT_EXISTS",
     "resource": "DRAFT"|"SCHEDULE"|"CONTENT"|null,
     "then_action": "UPDATE"|"PUBLISH"|null,
     "else_action": "CREATE"|null}
  ],
  "constraints": [
    {"type": "TIME"|"APPROVAL"|"USER_INPUT", "value": "..."}
  ],
  "target_hint": "reference to previous task/article (or null)",
  "confidence": 0.0-1.0
}

## Rules
1. "创建/写/生成/发布 文章/帖子/教程" → CREATE CONTENT
2. "修改/优化/完善/调整 文章/标题/内容" → UPDATE CONTENT (unless conditional)
3. "有则...无则.../如果...否则..." → CONDITIONAL mode + conditions[]
4. "搜索...分析...生成" (all for one goal) → COMPOSITE mode
5. "搜索/查找/找一下" alone → SEARCH POST
6. "发布/定时" → PUBLISH CONTENT or SCHEDULE
7. "发布前确认/审核后发布/让我审一下" → APPROVAL constraint
8. "刚才/上次/之前/第一篇" → target_hint
9. "取消/撤销" → DELETE SCHEDULE
10. Simple greeting/info question → QUERY (mode=SIMPLE)

## Examples

Input: "帮我运营一个Agent学习专题：先搜索最近热门文章并分析，如果之前有Agent学习草稿就优化，没有就创建，发布前让我确认，确认后五分钟发布"

Output:
{
  "mode": "CONDITIONAL",
  "goal": "运营 Agent 学习专题",
  "actions": [
    {"action":"SEARCH","resource":"POST"},
    {"action":"ANALYZE","resource":null},
    {"action":"CREATE","resource":"CONTENT"},
    {"action":"PUBLISH","resource":"CONTENT"}
  ],
  "conditions": [
    {"type":"IF_EXISTS","resource":"DRAFT","then_action":"UPDATE","else_action":"CREATE"}
  ],
  "constraints": [
    {"type":"APPROVAL","value":"BEFORE_PUBLISH"},
    {"type":"TIME","value":"5分钟后"}
  ],
  "target_hint": "Agent学习草稿",
  "confidence": 0.9
}

Input: "写一篇Java文章"
Output: {"mode":"SIMPLE","goal":"写Java文章","actions":[{"action":"CREATE","resource":"CONTENT"}],"conditions":[],"constraints":[],"target_hint":null,"confidence":0.95}

Input: "发布之前让我确认"
Output: {"mode":"SIMPLE","goal":"发布前确认","actions":[{"action":"PUBLISH","resource":"CONTENT"}],"conditions":[],"constraints":[{"type":"APPROVAL","value":"BEFORE_PUBLISH"}],"target_hint":null,"confidence":0.9}

Output valid JSON only. No markdown, no explanation."""
```

## 4.2 LLM 调用 (复用现有模式)

```python
async def _llm_understand_v2(self, text, existing_tasks) -> IntentSpec | None:
    if self._llm is None:
        return None

    resp = await self._llm.chat.completions.create(
        model=self._model,
        messages=[
            {"role": "system", "content": _L2_SYSTEM_V2},
            {"role": "user", "content": f"User: {text}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=500,
    )

    raw = resp.choices[0].message.content or "{}"
    return self._parse_intent_spec(raw)
```

# 5. IntentValidator

## 5.1 职责

```
Validator 不重新理解用户消息。
Validator 只检查 IntentSpec 内部一致性 + 与原文的结构一致性。
```

## 5.2 校验规则

```python
class IntentValidator:
    """Check IntentSpec for internal consistency."""

    def validate(self, spec: IntentSpec, original_text: str) -> IntentValidationResult:
        result = IntentValidationResult()

        # Rule 1: CONDITIONAL mode → must have conditions
        if spec.mode == IntentMode.CONDITIONAL and not spec.conditions:
            result.needs_repair = True
            result.errors.append("CONDITIONAL mode but no conditions defined")

        # Rule 2: Condition exists in text → mode should be CONDITIONAL
        if self._has_conditional_text(original_text) and spec.mode == IntentMode.SIMPLE:
            result.needs_repair = True
            result.errors.append("Text has conditional signals but mode is SIMPLE")

        # Rule 3: Resource hint in text → check action resource matches
        if "发布时间" in original_text:
            for a in spec.actions:
                if a.action == ActionType.UPDATE and a.resource != ResourceType.SCHEDULE:
                    result.needs_repair = True
                    result.suggested_fixes.append(
                        "Text mentions '发布时间' → UPDATE should target SCHEDULE"
                    )

        # Rule 4: "取消" in text → should have DELETE action
        if "取消" in original_text:
            has_delete = any(a.action == ActionType.DELETE for a in spec.actions)
            if not has_delete:
                result.warnings.append("Text has '取消' but no DELETE action")

        # Rule 5: actions should not be empty for non-QUERY mode
        if spec.mode != IntentMode.SIMPLE and not spec.actions:
            result.needs_repair = True
            result.errors.append("Non-SIMPLE mode with empty actions")

        # Rule 6: confidence should be reasonable
        if spec.confidence < 0.3:
            result.warnings.append("Very low confidence — consider L1 fallback")

        result.is_valid = not result.needs_repair
        return result

    @staticmethod
    def _has_conditional_text(text: str) -> bool:
        return bool(re.search(r"如果|否则|有则|无则|要是|假如", text))
```

# 6. 向后兼容: IntentSpec → TaskIntent

```python
def to_task_intent(spec: IntentSpec) -> TaskIntent:
    """Derive legacy TaskIntent from IntentSpec. Zero information loss on
    the legacy side; new fields (mode, conditions) are not yet consumed
    by downstream (Planner/Worker) in Stage A."""

    # relation: from first action
    primary = spec.actions[0].action if spec.actions else ActionType.QUERY
    relation_map = {
        ActionType.CREATE:  "NEW_TASK",
        ActionType.SEARCH:  "NEW_TASK",
        ActionType.ANALYZE: "NEW_TASK",
        ActionType.UPDATE:  "MODIFY_TASK",
        ActionType.DELETE:  "CANCEL_TASK",
        ActionType.QUERY:   "DIRECT",
        ActionType.PUBLISH: "NEW_TASK",
    }
    if spec.target_hint and primary == ActionType.UPDATE:
        relation = "MODIFY_TASK"
    else:
        relation = relation_map.get(primary, "NEW_TASK")

    # goal_category
    category_map = {
        ActionType.CREATE:  "CREATE_CONTENT",
        ActionType.UPDATE:  "IMPROVE_CONTENT",
        ActionType.SEARCH:  "ANALYZE_COMMUNITY",
        ActionType.ANALYZE: "ANALYZE_COMMUNITY",
        ActionType.PUBLISH: "PUBLISH_CONTENT",
        ActionType.DELETE:  "MANAGE_SCHEDULE",
        ActionType.QUERY:   "QUERY_INFO",
    }
    category = category_map.get(primary, "QUERY_INFO")

    # requirements
    req_type_map = {
        ActionType.SEARCH:  "SEARCH",
        ActionType.ANALYZE: "ANALYZE",
        ActionType.CREATE:  "CREATE",
        ActionType.UPDATE:  "IMPROVE",
        ActionType.PUBLISH: "PUBLISH",
        ActionType.DELETE:  "CANCEL",
    }
    reqs = [{"type": req_type_map[a.action]}
            for a in spec.actions if a.action in req_type_map]

    # resource_requests
    op_map = {
        ActionType.CREATE:  "CREATE",
        ActionType.UPDATE:  "UPDATE",
        ActionType.DELETE:  "DELETE",
        ActionType.PUBLISH: "CREATE",
    }
    res_map = {
        ResourceType.CONTENT:  "CONTENT_DRAFT",
        ResourceType.DRAFT:    "CONTENT_DRAFT",
        ResourceType.SCHEDULE: "SCHEDULE",
        ResourceType.POST:     "POST",
    }
    resource_reqs = [
        {"operation": op_map[a.action],
         "resource_type": res_map.get(a.resource, "CONTENT_DRAFT"),
         "hint": spec.target_hint or ""}
        for a in spec.actions
        if a.action in op_map and a.resource in res_map
    ]

    return TaskIntent(
        relation=relation,  # type: ignore[arg-type]
        goal=spec.goal,
        goal_category=category,
        target_task_hint=spec.target_hint,
        requirements=reqs,
        resource_requests=resource_reqs,
        constraints=[{"type": c.type.value, "value": c.value}
                      for c in spec.constraints],
        confidence=spec.confidence,
        source=spec.source,
        # NEW Phase 6.8.1 fields
        intent_spec=spec.model_dump(mode="json") if False else None,
    )
```

# 7. 最小文件修改清单 (Stage A)

| 操作 | 文件 | 变更行数 | 说明 |
|------|------|---------|------|
| **新增** | `task/intent_models.py` | ~90 | IntentSpec, IntentAction, IntentCondition, IntentConstraint, IntentValidationResult, ActionType, ResourceType, ConditionType, ConstraintType, IntentMode |
| **新增** | `task/intent_validator.py` | ~50 | IntentValidator (6 rules) |
| **新增** | `task/intent_compat.py` | ~60 | to_task_intent() 转换函数 |
| **修改** | `task/understanding.py` | +40 | L2 prompt v2; `_needs_l2_v2()`; `_llm_understand_v2()`; understand() 增加 Validator 步骤 |
| **修改** | `task/models.py` | +2 | TaskIntent +`intent_spec: dict | None` |
| **新增** | `tests/unit/test_intent_v2.py` | ~150 | 语义等价测试 |

### 不修改

```
548 个现有测试文件 — 零改动
以上 6 项之外的所有模块 — 零改动
```

# 8. 测试设计

## 8.1 语义等价测试矩阵

每个语义类别 3-5 个等价表达, 全部应产生相同 IntentSpec:

| 类别 | 等价表达 |
|------|---------|
| CREATE | "写一篇Java教程", "创建一个Python教程", "帮我搞个Spring Boot入门帖", "来一篇Agent学习内容" |
| QUERY | "查看我的草稿", "列一下之前的草稿", "看看未发布内容" |
| SCHEDULE_UPDATE | "把发布时间改到晚上9点", "明天那篇改成十点发", "调整任务的发布时间" |
| COMPOSITE | "搜索热门文章并整理路线", "找Java并发内容分析后写文章", "搜索、分析、生成并发布" |
| CONDITIONAL | "有旧文章就优化没有就创建", "找到草稿就完善找不到就重新写", "有Agent稿就接着改没有就新建" |
| HITL | "发布前让我确认", "审核通过以后再发", "先给我看确认后五分钟发布" |
| REFERENCE | "修改刚才那篇", "把第一篇改一下", "优化之前的Agent学习文章" |

## 8.2 评估指标

```
mode accuracy:
  SIMPLE / COMPOSITE / CONDITIONAL 分类正确率

action accuracy:
  actions[] 的类型集合正确率

resource accuracy:
  每个 action 的 resource 正确率

condition accuracy:
  conditions[] 的存在性和类型正确率

constraint accuracy:
  constraints[] 的类型正确率

overall intent accuracy:
  所有字段完全匹配的 case 比例
```

# 9. 风险点

| 风险 | 等级 | 缓解 |
|------|------|------|
| LLM Structured Output 不遵循 Schema | 🟡 中 | Pydantic 校验 + JSON repair + L1 fallback |
| L2 调用延迟增加 (~500ms) | 🟡 中 | 仅 20-30% 请求触发 L2; L1 快路径 <1ms |
| L2 输出与下游兼容性 | 🟢 低 | to_task_intent() 保证输出 TaskIntent 格式不变 |
| Validator 误判 needs_repair | 🟢 低 | Validator 只标记, 不阻止执行; repair 失败 → L1 fallback |
| 548 测试回归 | 🟢 低 | 新增代码, 不修改现有逻辑; L1 路径完全保留 |

# 10. 预期提升

| 类别 | 当前 (L1 only) | Stage A 目标 (L1+L2+Validator) |
|------|---------------|-------------------------------|
| SIMPLE | 67% | 83% |
| MODIFY | 80% | 100% |
| COMPOSITE | 80% | 100% |
| CONDITIONAL | 60% | 83% |
| HITL | 33% | 83% |
| **OVERALL** | **67%** | **88%** |
