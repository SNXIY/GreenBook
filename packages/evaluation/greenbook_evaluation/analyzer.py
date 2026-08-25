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

        check_occurrences: dict[str, int] = {}
        for check in failed_checks:
            ft = FailureAnalyzer._classify(check, result.category)
            occurrence = check_occurrences.get(check.check, 0)
            check_occurrences[check.check] = occurrence + 1
            assertion_id = f"{result.case_id}:{check.check}"
            if occurrence:
                assertion_id = f"{assertion_id}:{occurrence}"
            bad = BadCase(
                case_id=result.case_id,
                category=result.category,
                description=result.description,
                failure_type=ft,
                failure_reason=(
                    f"{check.check}: expected={check.expected}, "
                    f"actual={check.actual}"
                ),
                input=result.user_message,
                expected={"check": check.check, "value": check.expected},
                actual={"value": check.actual},
                trace_checks=[
                    {"check": c.check, "expected": c.expected, "actual": c.actual}
                    for c in result.checks
                ],
                assertion_id=assertion_id,
                failure_stage=FailureAnalyzer._failure_stage(check.check),
                root_cause_category=FailureAnalyzer._failure_stage(check.check),
            )
            bad_cases.append(bad)

        # If no specific checks failed but overall result is failed,
        # the errors list has the reason.
        if not bad_cases and result.errors:
            bad_cases.append(BadCase(
                case_id=result.case_id,
                category=result.category,
                input=result.user_message,
                failure_type=FailureType.UNKNOWN,
                failure_reason="; ".join(result.errors),
                assertion_id=f"{result.case_id}:error",
            ))

        return bad_cases

    @staticmethod
    def _failure_stage(check_name: str) -> str:
        return {
            "semantic_state": "INTERPRETER",
            "command": "INTERPRETER",
            "objective_count": "INTERPRETER",
            "temporal_resolution": "TEMPORAL",
            "target": "TARGET",
            "clarification": "CLARIFICATION",
            "tools": "TOOL",
            "task_state": "PROJECTION",
            "terminal_status": "PROJECTION",
            "resource_types": "PROJECTION",
            "schedule": "JAVA",
            "approval": "PROJECTION",
            "duplicate_write_count": "CONTINUATION",
            "ownership_conflicts": "TARGET",
            "side_effects": "JAVA",
            "actionloop": "ACTIONLOOP",
            "continuation": "CONTINUATION",
            "observability": "OBSERVABILITY",
        }.get(check_name, "INTERPRETER")

    # ── classification ───────────────────────────────────────────

    @staticmethod
    def _classify(check: EvalCheck, category: str) -> FailureType:
        """Map a check name + category to a FailureType."""
        check_name = check.check

        stage_types = {
            "semantic_state": FailureType.INTERPRETER,
            "objective_count": FailureType.INTERPRETER,
            "temporal_resolution": FailureType.TEMPORAL,
            "target": FailureType.TARGET,
            "clarification": FailureType.CLARIFICATION,
            "actionloop": FailureType.ACTIONLOOP,
            "side_effects": FailureType.JAVA,
            "task_state": FailureType.PROJECTION,
            "terminal_status": FailureType.PROJECTION,
            "resource_types": FailureType.PROJECTION,
            "schedule": FailureType.JAVA,
            "approval": FailureType.PROJECTION,
            "duplicate_write_count": FailureType.CONTINUATION,
            "ownership_conflicts": FailureType.TARGET,
            "continuation": FailureType.CONTINUATION,
            "observability": FailureType.OBSERVABILITY,
        }
        if check_name in stage_types:
            return stage_types[check_name]

        # Preserve the historical fine-grained EXECUTION report while the
        # new semantic/runtime categories use the canonical TOOL stage.
        if check_name == "tools":
            return (
                FailureType.WRONG_TOOL
                if str(category).upper() == "EXECUTION"
                else FailureType.TOOL
            )

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
