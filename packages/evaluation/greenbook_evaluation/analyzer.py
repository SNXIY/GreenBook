"""FailureAnalyzer — classify EvalResult failures into FailureTypes."""

from __future__ import annotations

from .badcase import BadCase, FailureType
from .models import EvalCheck, EvalResult


class FailureAnalyzer:
    """Classify a failed EvalResult into one or more BadCases."""

    # ── main entry ───────────────────────────────────────────────

    @staticmethod
    def analyze(result: EvalResult) -> list[BadCase]:
        """Analyze a failed result and produce BadCases."""
        if result.passed:
            return []

        bad_cases: list[BadCase] = []
        failed_checks = [c for c in result.checks if not c.ok]

        for check in failed_checks:
            ft = FailureAnalyzer._classify(check, result.category)
            bad = BadCase(
                case_id=result.case_id,
                category=result.category,
                description=result.description,
                failure_type=ft,
                failure_reason=(
                    f"{check.check}: expected={check.expected}, "
                    f"actual={check.actual}"
                ),
                input="",  # filled by caller
                expected={"check": check.check, "value": check.expected},
                actual={"value": check.actual},
                trace_checks=[
                    {"check": c.check, "expected": c.expected, "actual": c.actual}
                    for c in result.checks
                ],
            )
            bad_cases.append(bad)

        # If no specific checks failed but overall result is failed,
        # the errors list has the reason.
        if not bad_cases and result.errors:
            bad_cases.append(BadCase(
                case_id=result.case_id,
                category=result.category,
                failure_type=FailureType.UNKNOWN,
                failure_reason="; ".join(result.errors),
            ))

        return bad_cases

    # ── classification ───────────────────────────────────────────

    @staticmethod
    def _classify(check: EvalCheck, category: str) -> FailureType:
        """Map a check name + category to a FailureType."""
        check_name = check.check

        # Command understanding failures
        if check_name == "command.type":
            return FailureType.WRONG_CATEGORY
        if check_name == "command.action":
            return FailureType.WRONG_RELATION

        # Decomposition failures
        if check_name == "sub_task_count":
            expected = check.expected
            actual = check.actual
            if isinstance(expected, int) and isinstance(actual, int):
                if actual > expected:
                    return FailureType.OVER_SPLIT
                return FailureType.UNDER_SPLIT
            return FailureType.UNDER_SPLIT

        # Reference failures
        if check_name == "resource_id":
            return FailureType.WRONG_TASK
        if check_name == "clarification":
            return FailureType.AMBIGUITY_MISSED

        # Execution failures
        if check_name == "tools":
            return FailureType.WRONG_TOOL
        if check_name == "resource_id":
            return FailureType.MISSING_ARTIFACT

        # Catch-all
        return FailureType.UNKNOWN

    # ── summary ──────────────────────────────────────────────────

    @staticmethod
    def summary(bad_cases: list[BadCase]) -> dict[str, int]:
        """Count failures by FailureType."""
        counts: dict[str, int] = {}
        for bc in bad_cases:
            key = bc.failure_type.value
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))
