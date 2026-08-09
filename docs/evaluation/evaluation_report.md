# GreenBook Evaluation Report

## Purpose

GreenBook 的 Evaluation 层把质量问题拆成 Understanding、Planning 和 Execution 三个边界。评测只读取输入、结构化结果、计划、执行记录和事件，不修改 Runtime 状态，也不重新生成计划。

## Evaluation Entry Points

| Area | Implementation | Test / dataset entry |
| --- | --- | --- |
| Intent | `tests/evaluation/test_intent_v2_llm_eval.py`、`packages/evaluation/greenbook_evaluation/intent_dataset_v2.py` | `pytest tests/evaluation/test_intent_v2_llm_eval.py` |
| Planner | `packages/evaluation/greenbook_evaluation/planner_evaluator.py` | `PlannerEvaluator.evaluate(intent_spec, task_plan)` |
| Execution | `packages/evaluation/greenbook_evaluation/runtime_evaluator.py` | `ExecutionEvaluator.evaluate(execution_record)` |
| Aggregation | `packages/evaluation/greenbook_evaluation/metrics.py` | `MetricsCalculator`、`ExecutionMetricsCalculator` |
| Regression | `packages/evaluation/greenbook_evaluation/badcase.py` | `BadCaseStore` 和 `tests/evaluation/test_badcase.py` |

## Intent Metrics

Intent 评测以 `IntentSpec` 为主结果，legacy `TaskIntent` 只作为兼容投影单独统计。

| Metric | Definition |
| --- | --- |
| Mode Accuracy | 预测 `mode` 与标注模式一致的比例 |
| Action Coverage | 标注要求的 action 被 `IntentSpec.actions` 覆盖的比例；不使用整对象 exact match |
| Condition Accuracy | 条件存在且 `IF_EXISTS` / `IF_NOT_EXISTS` 类型符合标注的比例 |
| Constraint Accuracy | `APPROVAL`、`TIME` 等要求被正确表达的比例 |
| Repair Success | Validator 触发后，Targeted Repair 产生满足问题字段要求的最终 IntentSpec 的比例 |

现有 LLM evaluator 还记录 routing、resource、empty action、complex success、fallback 和 parse failure；这些作为诊断维度保留，不替代上述统一指标。

## Planner Metrics

`PlannerEvaluator` 对已经存在的 `IntentSpec` 和 `TaskPlan` 做确定性检查：

| Metric | Definition |
| --- | --- |
| Action Coverage | Plan capability 映射覆盖 IntentSpec action 的比例 |
| Resource Match | 计划输入/输出 artifact 与 IntentSpec resource 是否匹配 |
| Step Ordering | 依赖方向和业务先后顺序是否合理 |
| Constraint Propagation | IntentSpec constraint 是否出现在对应计划步骤 |

Planner Evaluation 不调用 Planner，不修复失败计划，避免评测污染被测对象。

## Execution Metrics

`ExecutionEvaluator` 输入 `ExecutionRecord`，其中包含 `PlanExecution`、`ExecutionEvent`、`StepExecution` 和可选 Trace：

| Metric | Definition |
| --- | --- |
| Success Rate | `COMPLETED` execution 占全部 execution 的比例 |
| Failure Rate | 存在失败 step 或失败事件的 execution 占比 |
| Retry Rate | `retry_count > 0` 的 execution 占比 |
| Latency | execution 创建到完成/更新时间的毫秒数；聚合时取平均值 |
| Human Approval Rate | 存在 `APPROVAL_REQUIRED` 或 `WAITING_HUMAN` 的 execution 占比 |

实现还记录 step count、failure count、tool call count、tool failure rate 和 quality score，便于定位失败来源。

## Badcase and Regression

失败案例使用 `BadCase` 保存：

- user input；
- IntentSpec；
- TaskPlan；
- execution trace；
- failure reason；
- expected behavior。

Badcase 不进入生产执行链。修复后应将原案例加入 regression dataset，分别验证 Intent、Planner 和 Execution 边界，防止只提升最终成功率却重新引入信息损失。

## Running Evaluation

离线确定性评测：

```powershell
pytest tests/evaluation/test_models.py tests/evaluation/test_eval_runner.py tests/evaluation/test_badcase.py
```

完整 Intent LLM 评测需要配置 `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL` 和 `LLM_MODEL`：

```powershell
pytest tests/evaluation/test_intent_v2_llm_eval.py -s
```

未配置外部模型时，该测试会跳过或因当前环境缺少 `openai` 依赖而不能执行；报告不得将这种情况记为模型质量指标。

## Quality Boundary

```text
IntentSpec quality
        -> Planner quality
        -> Execution quality
        -> Badcase regression
```

评测层只观察和归因。IntentSpec、Planner、Worker、ToolRuntime 和 ExecutionStateManager 的行为仍由各自正式模块负责。

