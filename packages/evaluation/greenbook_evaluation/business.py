"""Lightweight Business Acceptance evaluation over the production semantic path."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any

from .canonical import semantic_mapping_matches, semantic_values_equal
from .models import EvalCase


@dataclass
class BusinessAcceptanceRun:
    case_id: str
    passed: bool = False
    infra_error: bool = False
    error: str = ""
    actual: dict[str, Any] = field(default_factory=dict)
    checks: dict[str, bool] = field(default_factory=dict)


@dataclass
class BusinessAcceptanceReport:
    case_count: int
    valid_case_count: int
    pass_count: int
    fail_count: int
    infra_error_count: int
    results: list[BusinessAcceptanceRun]
    metrics: dict[str, dict[str, int | float]]

    @property
    def semantic_business_correctness(self) -> float:
        return _rate(self.metrics["semantic_business_correctness"])

    @property
    def objective_count_correctness(self) -> float:
        return _rate(self.metrics["objective_count_correctness"])

    @property
    def target_correctness(self) -> float:
        return _rate(self.metrics["target_correctness"])

    @property
    def temporal_correctness(self) -> float:
        return _rate(self.metrics["temporal_correctness"])

    @property
    def publication_correctness(self) -> float:
        return _rate(self.metrics["publication_correctness"])


class BusinessAcceptanceEvaluator:
    """Run cases one time each and keep infrastructure failures separate."""

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter

    async def evaluate(self, cases: list[EvalCase]) -> BusinessAcceptanceReport:
        results: list[BusinessAcceptanceRun] = []
        metric_names = (
            "semantic_business_correctness",
            "objective_count_correctness",
            "target_correctness",
            "temporal_correctness",
            "publication_correctness",
        )
        metrics = {name: {"correct": 0, "total": 0} for name in metric_names}

        for case in cases:
            try:
                value = self.adapter.run_case(case)
                actual = await value if inspect.isawaitable(value) else value
                if hasattr(actual, "model_dump"):
                    actual = actual.model_dump(mode="json")
                if not isinstance(actual, dict):
                    raise TypeError("Business semantic adapter must return a mapping")
            except Exception as exc:  # noqa: BLE001 - classify at the evaluation boundary
                if _is_infra_error(exc):
                    results.append(BusinessAcceptanceRun(
                        case_id=case.case_id,
                        infra_error=True,
                        error=str(exc),
                    ))
                    continue
                results.append(BusinessAcceptanceRun(
                    case_id=case.case_id,
                    error=str(exc),
                ))
                continue

            expected = dict(case.expected_semantic_state or {})
            semantic = dict(actual.get("semantic_state") or {})
            checks = _checks(case, expected, semantic, actual)
            for name, ok in checks.items():
                metrics[name]["total"] += 1
                metrics[name]["correct"] += int(ok)
            results.append(BusinessAcceptanceRun(
                case_id=case.case_id,
                passed=all(checks.values()),
                actual=actual,
                checks=checks,
            ))

        valid = len(results) - sum(result.infra_error for result in results)
        passed = sum(result.passed for result in results if not result.infra_error)
        return BusinessAcceptanceReport(
            case_count=len(cases),
            valid_case_count=valid,
            pass_count=passed,
            fail_count=valid - passed,
            infra_error_count=sum(result.infra_error for result in results),
            results=results,
            metrics=metrics,
        )

    def evaluate_sync(self, cases: list[EvalCase]) -> BusinessAcceptanceReport:
        return asyncio.run(self.evaluate(cases))


def _checks(
    case: EvalCase,
    expected: dict[str, Any],
    semantic: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, bool]:
    expected_count = expected.get("objective_count")
    actual_count = semantic.get("objective_count", actual.get("objective_count"))
    target_expected = expected.get("target_state")
    target_actual = semantic.get("target_state")
    temporal_expected = (
        expected.get("temporal_kind"),
        bool(expected.get("temporal_resolved")),
    )
    temporal_actual = (
        semantic.get("temporal_kind"),
        bool(semantic.get("temporal_resolved")),
    )
    publication_expected = expected.get("publication_mode")
    publication_actual = semantic.get("publication_mode")
    return {
        "semantic_business_correctness": semantic_mapping_matches(expected, semantic),
        "objective_count_correctness": semantic_values_equal(
            expected_count, actual_count, field="objective_count"
        ),
        "target_correctness": semantic_values_equal(target_expected, target_actual, field="target_state"),
        "temporal_correctness": temporal_expected == temporal_actual,
        "publication_correctness": semantic_values_equal(
            publication_expected, publication_actual, field="publication_mode"
        ),
    }


def _rate(metric: dict[str, int | float]) -> float:
    total = int(metric.get("total", 0))
    return int(metric.get("correct", 0)) / total if total else 0.0


def _is_infra_error(exc: Exception) -> bool:
    """Classify transport/provider failures without hiding semantic failures."""

    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    if status is not None:
        try:
            return int(status) in {402, 408, 409, 429} or int(status) >= 500
        except (TypeError, ValueError):
            pass
    code = str(getattr(exc, "code", "") or "").upper()
    message = str(exc).upper()
    if code in {"COMMAND_LLM_UNAVAILABLE", "LLM_UNAVAILABLE", "API_UNAVAILABLE"}:
        return True
    if any(marker in message for marker in (
        "TIMEOUT", "TIMED OUT", "CONNECTION", "CONNECTERROR", "RATE LIMIT",
        "TOO MANY REQUESTS", "BAD GATEWAY", "SERVICE UNAVAILABLE", "502", "503", "504",
    )):
        return True
    return False


__all__ = [
    "BusinessAcceptanceEvaluator",
    "BusinessAcceptanceReport",
    "BusinessAcceptanceRun",
]
