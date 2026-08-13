"""EvalRunner — execute EvalCases against the Runtime."""

from __future__ import annotations

import inspect
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from .models import (
    EvalCase,
    EvalCheck,
    EvalResult,
    EvaluationReport,
    EvaluationTrace,
    FailureCategory,
)


class MockMCP:
    """Mock MCP that returns canned responses keyed by tool name."""

    def __init__(self, responses: dict[str, dict] | None = None):
        self._responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> dict:
        self.calls.append((tool_name, kwargs))
        if tool_name in self._responses:
            return dict(self._responses[tool_name])
        # Default success for any tool
        return {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-mock", "title": "Mock Result"},
        }


_DEFAULT_MCP_RESPONSES: dict[str, dict] = {
    "content.create_draft": {
        "ok": True, "code": "",
        "data": {"draft_id": "draft-eval", "title": "Eval Article"},
    },
    "content.revise_draft": {
        "ok": True, "code": "",
        "data": {"draft_id": "draft-eval", "title": "Revised", "status": "DRAFT"},
    },
    "community.search_public_posts": {
        "ok": True, "code": "",
        "data": {"items": [{"post_id": "p1", "title": "Hot Java"}], "total": 1},
    },
    "publication.schedule": {
        "ok": True, "code": "",
        "data": {"schedule_id": "sched-eval", "draft_id": "draft-eval",
                 "status": "SCHEDULED"},
    },
    "publication.update_schedule": {
        "ok": True, "code": "",
        "data": {"schedule_id": "sched-eval", "status": "SCHEDULED"},
    },
    "publication.cancel_schedule": {
        "ok": True, "code": "",
        "data": {"schedule_id": "sched-eval", "status": "CANCELLED"},
    },
}


ActualCaseHandler = Callable[[EvalCase], Any | Awaitable[Any]]


class EvaluationRunner:
    """Run behavioral golden cases against a fake or real Runtime adapter.

    The runner deliberately accepts an injected handler.  A deterministic
    fake LLM/tool runtime can be used in unit tests, while integration tests
    can pass the ConversationRuntime adapter.  No result is fabricated when
    no adapter is configured.
    """

    def __init__(
        self,
        runtime: Any | None = None,
        *,
        fake_llm: Any | None = None,
        fake_tool_runtime: Any | None = None,
    ) -> None:
        self._runtime = runtime
        self.fake_llm = fake_llm
        self.fake_tool_runtime = fake_tool_runtime

    async def run_case(
        self,
        case: EvalCase,
        *,
        actual: Any | None = None,
        handler: ActualCaseHandler | None = None,
    ) -> EvalResult:
        started = time.monotonic()
        try:
            if actual is None:
                actual = await self._execute_case(case, handler)
            payload = _payload(actual)
            checks = _behavior_checks(case, payload)
            failed = [check for check in checks if not check.ok]
            categories = [_failure_category(check.check).value for check in failed]
            trace = EvaluationTrace.model_validate(payload.get("trace") or payload)
            metrics = _case_metrics(case, payload, checks)
            elapsed = (time.monotonic() - started) * 1000.0
            return EvalResult(
                case_id=case.case_id,
                category=case.category,
                description=case.description,
                passed=not failed,
                checks=checks,
                errors=[str(payload.get("error", ""))] if payload.get("error") else [],
                duration_ms=elapsed,
                trace=trace.model_dump(mode="json"),
                trace_summary={
                    "event_count": len(trace.events),
                    "tool_count": int(payload.get("tool_call_count", len(payload.get("tools", [])))),
                    "step_count": len(payload.get("steps", [])),
                },
                failure_categories=list(dict.fromkeys(categories)),
                metrics=metrics,
            )
        except Exception as exc:
            elapsed = (time.monotonic() - started) * 1000.0
            return EvalResult(
                case_id=case.case_id,
                category=case.category,
                description=case.description,
                passed=False,
                errors=[str(exc)],
                duration_ms=elapsed,
                failure_categories=[FailureCategory.EXECUTION_ERROR.value],
            )

    async def run_cases(
        self,
        cases: Sequence[EvalCase],
        *,
        handler: ActualCaseHandler | None = None,
        run_id: str = "agent-eval",
    ) -> EvaluationReport:
        results = [await self.run_case(case, handler=handler) for case in cases]
        from .metrics import EvaluationMetricsCalculator

        passed = sum(1 for result in results if result.passed)
        metrics = EvaluationMetricsCalculator.compute(results)
        return EvaluationReport(
            run_id=run_id,
            total_cases=len(results),
            total_passed=passed,
            overall_accuracy=passed / len(results) if results else 0.0,
            results=results,
            metrics=metrics.model_dump(mode="json"),
        )

    async def run_dataset(
        self,
        cases: Sequence[EvalCase],
        *,
        handler: ActualCaseHandler | None = None,
    ) -> EvaluationReport:
        return await self.run_cases(cases, handler=handler)

    def run_sync(
        self,
        cases: Sequence[EvalCase],
        *,
        handler: ActualCaseHandler | None = None,
    ) -> EvaluationReport:
        import asyncio

        return asyncio.run(self.run_cases(cases, handler=handler))

    async def _execute_case(
        self,
        case: EvalCase,
        handler: ActualCaseHandler | None,
    ) -> Any:
        target = handler or self._runtime
        if target is None:
            raise RuntimeError("EvaluationRunner requires a Runtime or case handler.")
        if callable(target):
            result = target(case)
        elif callable(getattr(target, "run_case", None)):
            result = target.run_case(case)
        elif callable(getattr(target, "run", None)):
            result = target.run(case)
        else:
            raise TypeError("Evaluation runtime must be callable or expose run_case/run.")
        return await result if inspect.isawaitable(result) else result


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {"value": dumped}
    return {"value": value}


def _behavior_checks(case: EvalCase, actual: Mapping[str, Any]) -> list[EvalCheck]:
    checks: list[EvalCheck] = []
    if case.expected_command:
        value = actual.get("command", actual.get("command_type", ""))
        if isinstance(value, Mapping):
            value = value.get("type", value.get("command", ""))
        checks.append(EvalCheck(check="command", expected=case.expected_command, actual=value, ok=str(value).upper() == case.expected_command.upper()))
    if case.expected_target is not None:
        value = actual.get("target", actual.get("resolved_target", {})) or {}
        ok = all(value.get(key) == expected for key, expected in case.expected_target.items())
        checks.append(EvalCheck(check="target", expected=case.expected_target, actual=value, ok=ok))
    if case.expected_goals:
        values = actual.get("goals", actual.get("goal_types", [])) or []
        names = {
            str(item.get("goal_type", item.get("kind", item.get("name", ""))))
            if isinstance(item, Mapping) else str(item)
            for item in values
        }
        ok = set(case.expected_goals) <= names
        checks.append(EvalCheck(check="goals", expected=case.expected_goals, actual=values, ok=ok))
    if case.expected_tools:
        values = [str(item) for item in (actual.get("tools", actual.get("selected_tools", [])) or [])]
        ok = set(case.expected_tools) <= set(values)
        checks.append(EvalCheck(check="tools", expected=case.expected_tools, actual=values, ok=ok))
    expected_status = case.expected_task_state
    if expected_status:
        value = str(actual.get("task_state", actual.get("status", ""))).upper()
        checks.append(EvalCheck(check="task_state", expected=expected_status, actual=value, ok=value == expected_status.upper()))
    if case.expected_artifacts:
        values = [str(item) for item in (actual.get("artifacts", []) or [])]
        checks.append(EvalCheck(check="artifacts", expected=case.expected_artifacts, actual=values, ok=set(case.expected_artifacts) <= set(values)))
    if case.expected_side_effects:
        values = set(str(item) for item in (actual.get("side_effects", []) or []))
        safe = not bool(actual.get("duplicate_side_effect", False))
        ok = safe and ("NO_DUPLICATE_PUBLICATION" in case.expected_side_effects or set(case.expected_side_effects) <= values)
        checks.append(EvalCheck(check="side_effects", expected=case.expected_side_effects, actual=list(values), ok=ok))
    if case.forbidden_actions:
        values = set(str(item) for item in (actual.get("actions", []) or []))
        checks.append(EvalCheck(check="forbidden_actions", expected=case.forbidden_actions, actual=list(values), ok=not values.intersection(case.forbidden_actions)))
    return checks


def _failure_category(check: str) -> FailureCategory:
    return {
        "command": FailureCategory.COMMAND_ERROR,
        "target": FailureCategory.TARGET_ERROR,
        "goals": FailureCategory.GOAL_ERROR,
        "tools": FailureCategory.TOOL_SELECTION_ERROR,
        "task_state": FailureCategory.EXECUTION_ERROR,
        "side_effects": FailureCategory.RECOVERY_ERROR,
        "forbidden_actions": FailureCategory.HALLUCINATION,
    }.get(check, FailureCategory.EXECUTION_ERROR)


def _case_metrics(
    case: EvalCase,
    actual: Mapping[str, Any],
    checks: Sequence[EvalCheck],
) -> dict[str, float]:
    values = {check.check: float(check.ok) for check in checks}
    values["context_continuity"] = float(bool(actual.get("context_continuity", True)))
    values["long_conversation_consistency"] = float(
        bool(actual.get("long_conversation_consistency", values["context_continuity"]))
    )
    values["memory_retrieval_precision"] = float(bool(actual.get("memory_retrieval_ok", True)))
    values["replan_recovery"] = float(bool(actual.get("replan_recovered", True)))
    values["recovery_success"] = float(bool(actual.get("recovery_success", values["replan_recovery"])))
    values["idempotent_recovery"] = float(not bool(actual.get("duplicate_side_effect", False)))
    values["task_success"] = float(
        bool(actual.get("task_success", actual.get("task_state") in {"COMPLETED", "SUCCESS"}))
    )
    values["multi_task"] = float(
        bool(actual.get("multi_task_accuracy", actual.get("multi_task", True)))
    )
    values["plan_quality"] = float(bool(actual.get("plan_quality", True)))
    values["latency_ms"] = float(actual.get("latency_ms", 0.0) or 0.0)
    values["tool_call_count"] = float(actual.get("tool_call_count", len(actual.get("tools", []) or [])))
    return values
