"""Evaluation of decomposition, goals, dependencies, and Agent routing."""

from __future__ import annotations

from typing import Any


def compare_task_graph(expected: dict[str, Any], actual: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for field in ("expected_tasks", "expected_goals", "expected_dependencies", "expected_agents"):
        expected_value = expected.get(field)
        if expected_value is None:
            continue
        actual_field = field.removeprefix("expected_")
        actual_value = actual.get(actual_field, actual.get(field, []))
        if isinstance(expected_value, int) and isinstance(actual_value, list):
            actual_value = len(actual_value)
        checks.append({
            "metric": {
                "expected_tasks": "task_decomposition_accuracy",
                "expected_goals": "task_decomposition_accuracy",
                "expected_dependencies": "planner_accuracy",
                "expected_agents": "planner_accuracy",
            }[field],
            "field": field,
            "expected": expected_value,
            "actual": actual_value,
            "ok": _normalise(expected_value) == _normalise(actual_value),
        })
    return checks


def _normalise(value: Any) -> Any:
    if isinstance(value, list):
        return [_normalise(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _normalise(item) for key, item in sorted(value.items())}
    return value
