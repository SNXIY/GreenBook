"""MetricsCalculator — compute accuracy, per-category stats, and badcase analysis."""

from __future__ import annotations

from .analyzer import FailureAnalyzer
from .badcase import BadCase
from .models import AgentEvaluationMetrics, CategoryMetrics, EvalResult, EvaluationReport


class ExecutionMetrics:
    """Aggregate runtime quality indicators over execution evaluations."""

    def __init__(self, **values: float) -> None:
        self.execution_success_rate = values.get("execution_success_rate", 0.0)
        self.average_latency = values.get("average_latency", 0.0)
        self.retry_rate = values.get("retry_rate", 0.0)
        self.failure_rate = values.get("failure_rate", 0.0)
        self.human_approval_rate = values.get("human_approval_rate", 0.0)
        self.tool_failure_rate = values.get("tool_failure_rate", 0.0)

    def model_dump(self) -> dict[str, float]:
        return self.__dict__.copy()


class ExecutionMetricsCalculator:
    @staticmethod
    def compute(evaluations: list[object]) -> ExecutionMetrics:
        total = len(evaluations)
        if not total:
            return ExecutionMetrics()
        tool_calls = sum(int(item.tool_call_count) for item in evaluations)
        tool_failures = sum(int(getattr(item, "tool_failure_count", 0)) for item in evaluations)
        return ExecutionMetrics(
            execution_success_rate=sum(bool(item.success) for item in evaluations) / total,
            average_latency=sum(float(item.latency) for item in evaluations) / total,
            retry_rate=sum(item.retry_count > 0 for item in evaluations) / total,
            failure_rate=sum(item.failure_count > 0 for item in evaluations) / total,
            human_approval_rate=sum(bool(item.human_intervention) for item in evaluations) / total,
            tool_failure_rate=tool_failures / tool_calls if tool_calls else 0.0,
        )


class MetricsCalculator:
    """Compute evaluation metrics from a list of EvalResults."""

    @staticmethod
    def compute(
        results: list[EvalResult], dataset_name: str = "",
    ) -> EvaluationReport:
        if not results:
            return EvaluationReport()

        total = len(results)
        passed = sum(1 for r in results if r.passed)

        # Per-category breakdown
        by_category: dict[str, CategoryMetrics] = {}
        for r in results:
            cat = r.category
            if cat not in by_category:
                by_category[cat] = CategoryMetrics(category=cat)
            by_category[cat].total += 1
            if r.passed:
                by_category[cat].passed += 1
            else:
                by_category[cat].failures.append(r.case_id)

        for cat_m in by_category.values():
            cat_m.accuracy = (
                cat_m.passed / cat_m.total if cat_m.total > 0 else 0.0
            )

        # Badcase analysis
        all_bad_cases: list[BadCase] = []
        for r in results:
            if not r.passed:
                all_bad_cases.extend(FailureAnalyzer.analyze(r))
        failure_summary = FailureAnalyzer.summary(all_bad_cases)

        return EvaluationReport(
            run_id=dataset_name or "eval",
            total_cases=total,
            total_passed=passed,
            overall_accuracy=passed / total if total > 0 else 0.0,
            by_category=by_category,
            results=results,
            bad_cases=all_bad_cases,
            failure_summary=failure_summary,
        )


class EvaluationMetricsCalculator:
    """Aggregate behavioral metrics from ``EvaluationRunner`` results."""

    @staticmethod
    def compute(results: list[EvalResult]) -> AgentEvaluationMetrics:
        if not results:
            return AgentEvaluationMetrics()

        def rate(name: str, *, default: float = 0.0) -> float:
            values = [result.metrics.get(name, default) for result in results]
            return sum(values) / len(values) if values else 0.0

        return AgentEvaluationMetrics(
            command_accuracy=rate("command"),
            target_resolution_accuracy=rate("target"),
            goal_decomposition_accuracy=rate("goals"),
            tool_selection_accuracy=rate("tools"),
            task_success_rate=rate("task_success", default=rate("task_state")),
            task_completion_rate=rate("task_state"),
            plan_quality=rate("plan_quality", default=rate("plan_success", default=rate("task_state"))),
            plan_success_rate=rate("plan_success", default=rate("task_state")),
            recovery_success=rate("recovery_success", default=rate("replan_recovery", default=1.0)),
            replan_recovery_rate=rate("replan_recovery", default=1.0),
            multi_task_accuracy=rate("multi_task", default=rate("goals")),
            long_conversation_consistency=rate("long_conversation_consistency", default=rate("context_continuity", default=1.0)),
            clarification_precision=rate("clarification", default=1.0),
            side_effect_safety=rate("side_effects", default=1.0),
            idempotent_recovery=rate("idempotent_recovery", default=1.0),
            memory_retrieval_precision=rate("memory_retrieval_precision", default=1.0),
            context_continuity=rate("context_continuity", default=1.0),
            average_latency_ms=rate("latency_ms"),
            average_tool_call_count=rate("tool_call_count"),
        )


AgentMetricsCalculator = EvaluationMetricsCalculator
