# Phase 6.3 — Agent Evaluation Platform 设计

> 日期: 2026-08-07
> 状态: 设计阶段 — 确认后编码

---

# 1. 评测目标

| 能力 | 评测内容 | 示例 |
|------|---------|------|
| Intent Understanding | TaskIntent 是否正确 | "优化一下" → IMPROVE_CONTENT |
| Task Decomposition | 拆分是否正确 | "创建A然后创建B" → 2 SubTasks |
| Reference Resolution | 引用解析是否正确 | "昨天那个文章" → task_id |
| Resource Binding | 资源绑定是否正确 | "修改Java文章" → draft_id |
| Plan Generation | 模板选择是否正确 | CREATE+PUBLISH → CREATE_AND_PUBLISH |
| Tool Selection | 工具选择是否正确 | SCHEDULE_PUBLISH → publication.schedule |
| Execution Reliability | 端到端执行成功率 | pipeline complete without error |

# 2. 数据模型

## 2.1 EvalCase

```python
class EvalCase(BaseModel):
    """One test case."""
    case_id: str
    category: str              # INTENT | DECOMPOSITION | REFERENCE | …
    description: str           # human-readable
    user_message: str

    # Conversation context (tasks, artifacts from prior rounds)
    existing_tasks: list[dict] = []
    # [{task_id, goal, goal_category, artifacts: [{type, resource_id}]}]

    # Expected outputs (partial — only assert what's relevant)
    expected_intent: dict | None = None
    # {goal_category, relation, requirements, resource_requests}

    expected_sub_task_count: int | None = None
    expected_template: str | None = None
    expected_tools: list[str] | None = None       # tool names called
    expected_resource_id: str | None = None
    expected_reference_task_id: str | None = None

    should_succeed: bool = True
```

## 2.2 EvalResult

```python
class EvalResult(BaseModel):
    """Result of running one EvalCase."""
    case_id: str
    category: str
    passed: bool
    checks: list[dict] = []        # [{check: "intent.goal_category", expected, actual, ok}]
    errors: list[str] = []
    duration_ms: float = 0.0
    trace_summary: dict = {}       # {event_count, tool_count, step_count}
```

## 2.3 Metrics

```python
class CategoryMetrics(BaseModel):
    """Per-category accuracy."""
    category: str
    total: int = 0
    passed: int = 0
    accuracy: float = 0.0
    failures: list[str] = []      # case_ids that failed

class EvaluationReport(BaseModel):
    """Complete evaluation report."""
    run_id: str
    total_cases: int = 0
    total_passed: int = 0
    overall_accuracy: float = 0.0
    by_category: dict[str, CategoryMetrics] = {}
    duration_ms: float = 0.0
    results: list[EvalResult] = []
```

# 3. 架构设计

## 3.1 模块结构

```
packages/evaluation/greenbook_evaluation/
    __init__.py
    models.py           # EvalCase, EvalResult, EvaluationReport, CategoryMetrics
    datasets.py         # built-in datasets (intent, decomposition, reference, …)
    runner.py           # EvalRunner — executes cases, collects results
    metrics.py          # MetricsCalculator — computes accuracy, breakdowns
```

## 3.2 EvalRunner

```python
class EvalRunner:
    """Execute EvalCases against the Runtime and collect results."""

    def __init__(self, ras: RuntimeAgentService):
        self._ras = ras

    async def run(self, dataset: list[EvalCase]) -> EvaluationReport:
        results = []
        for case in dataset:
            result = await self._run_one(case)
            results.append(result)
        return MetricsCalculator.compute(results)

    async def _run_one(self, case: EvalCase) -> EvalResult:
        """Execute one case and check against expectations."""
        # 1. Build RuntimeContext from case
        ctx = self._build_context(case)

        # 2. Execute
        t0 = time.monotonic()
        result = await self._ras.execute(ctx)
        elapsed = (time.monotonic() - t0) * 1000

        # 3. Check expectations
        checks = []
        if case.expected_intent:
            checks.extend(self._check_intent(case.expected_intent, ctx.task_intent))
        if case.expected_sub_task_count is not None:
            checks.append(self._check("sub_task_count",
                                       case.expected_sub_task_count,
                                       len(result.sub_tasks or [])))
        if case.expected_template:
            checks.append(self._check("template", case.expected_template,
                                       result.template_name))
        # ...

        passed = all(c["ok"] for c in checks)
        return EvalResult(
            case_id=case.case_id, category=case.category,
            passed=passed, checks=checks, duration_ms=elapsed,
        )
```

## 3.3 依赖注入

```
EvalRunner 不依赖 HTTP 层。
直接构造 RuntimeContext + Mock MCP 即可运行完整管线。

RuntimeContext
  ├── task_intent (from case or empty)
  ├── recent_tasks (from case.existing_tasks)
  ├── mcp (mock)
  ├── llm (mock — L1 only, no API calls)
  └── user_message (from case.user_message)
```

# 4. 数据集设计

## 4.1 Intent Understanding (20 cases)

```python
INTENT_DATASET = [
    EvalCase(
        case_id="intent-01", category="INTENT",
        description="明确创建",
        user_message="帮我写一篇Java文章",
        expected_intent={"goal_category": "CREATE_CONTENT", "relation": "NEW_TASK"},
    ),
    EvalCase(
        case_id="intent-02", category="INTENT",
        description="语义变体: 优化",
        user_message="参考热门Java帖子优化刚才文章",
        expected_intent={"goal_category": "IMPROVE_CONTENT", "relation": "MODIFY_TASK"},
    ),
    EvalCase(
        case_id="intent-03", category="INTENT",
        description="语义变体: 完善",
        user_message="完善一下刚才那篇",
        expected_intent={"goal_category": "IMPROVE_CONTENT"},
    ),
    # ... 17 more
]
```

## 4.2 Task Decomposition (10 cases)

```python
DECOMPOSITION_DATASET = [
    EvalCase(
        case_id="decomp-01", category="DECOMPOSITION",
        description="两个独立创建",
        user_message="写Java文章。然后写Python文章。",
        expected_sub_task_count=2,
    ),
    EvalCase(
        case_id="decomp-02", category="DECOMPOSITION",
        description="单任务多步骤(不拆分)",
        user_message="搜索Java帖子然后分析原因然后生成文章",
        expected_sub_task_count=1,
    ),
    # ... 8 more
]
```

## 4.3 Reference Resolution (10 cases)

```python
REFERENCE_DATASET = [
    EvalCase(
        case_id="ref-01", category="REFERENCE",
        description="昨天文章",
        user_message="修改昨天那篇文章标题",
        existing_tasks=[
            {"task_id": "t1", "goal": "创建Java文章", "goal_category": "CREATE_CONTENT",
             "created_at_ago": 108000},  # 30h ago
        ],
        expected_reference_task_id="t1",
    ),
    # ... 9 more
]
```

## 4.4 Resource Binding (10 cases)

```python
RESOURCE_DATASET = [
    EvalCase(
        case_id="res-01", category="RESOURCE",
        description="找到正确 draft",
        user_message="修改Java文章标题",
        existing_tasks=[
            {"task_id": "t1", "goal": "创建Java文章",
             "artifacts": [{"type": "DRAFT", "resource_id": "draft-java"}]},
        ],
        expected_resource_id="draft-java",
    ),
    # ... 9 more
]
```

## 4.5 Plan Generation (10 cases)

```python
PLAN_DATASET = [
    EvalCase(
        case_id="plan-01", category="PLAN",
        description="CREATE+PUBLISH → CREATE_AND_PUBLISH",
        user_message="写Java文章，明天发布",
        expected_template="CREATE_AND_PUBLISH",
    ),
    # ... 9 more
]
```

## 4.6 Execution (10 cases)

```python
EXECUTION_DATASET = [
    EvalCase(
        case_id="exec-01", category="EXECUTION",
        description="简单创建成功",
        user_message="写一篇Java文章",
        expected_tools=["content.create_draft"],
        should_succeed=True,
    ),
    # ... 9 more
]
```

# 5. 修改文件

| 操作 | 文件 | 说明 |
|------|------|------|
| **新增** | `packages/evaluation/greenbook_evaluation/__init__.py` | package marker |
| **新增** | `packages/evaluation/greenbook_evaluation/models.py` | EvalCase, EvalResult, EvaluationReport |
| **新增** | `packages/evaluation/greenbook_evaluation/datasets.py` | 6 个内置数据集 (~70 cases) |
| **新增** | `packages/evaluation/greenbook_evaluation/runner.py` | EvalRunner |
| **新增** | `packages/evaluation/greenbook_evaluation/metrics.py` | MetricsCalculator |
| **新增** | `tests/evaluation/test_eval_runner.py` | EvalRunner 本身的测试 |

### 不修改

```
所有现有模块 — 零改动
```

# 6. 风险分析

| 风险 | 等级 | 缓解 |
|------|------|------|
| Mock 环境与真实环境差异 | 🟡 中 | Mock MCP 使用 canned responses; EvalRunner 也支持真实 MCP |
| 评测案例主观性 | 🟢 低 | 每个 case 只断言可客观验证的字段 (goal_category, template_name, tool_name) |
| L2 LLM 结果不稳定 | 🟡 中 | 默认使用 L1 (确定性); L2 评测用统计指标 (accuracy@5) |
| 数据集膨胀 | 🟢 低 | 按类别组织, 每类 10-20 cases |

# 7. 实施步骤

| Step | 内容 | 时间 |
|------|------|------|
| 1 | `evaluation/models.py` | 30min |
| 2 | `evaluation/datasets.py` (intent + decomposition 数据集) | 1h |
| 3 | `evaluation/runner.py` | 1h |
| 4 | `evaluation/metrics.py` | 30min |
| 5 | 4 个剩余数据集 | 1h |
| 6 | 测试 EvalRunner | 30min |
