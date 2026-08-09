"""BadCase model — a single failure record with classification."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class FailureType(StrEnum):
    # Intent
    WRONG_CATEGORY = "WRONG_CATEGORY"
    WRONG_RELATION = "WRONG_RELATION"

    # Decomposition
    OVER_SPLIT = "OVER_SPLIT"
    UNDER_SPLIT = "UNDER_SPLIT"
    WRONG_DEPENDENCY = "WRONG_DEPENDENCY"

    # Reference
    WRONG_TASK = "WRONG_TASK"
    AMBIGUITY_MISSED = "AMBIGUITY_MISSED"

    # Execution
    WRONG_TOOL = "WRONG_TOOL"
    MISSING_ARTIFACT = "MISSING_ARTIFACT"
    RECOVERY_FAILED = "RECOVERY_FAILED"

    # Uncategorised
    UNKNOWN = "UNKNOWN"


class BadCase(BaseModel):
    """One failure from an evaluation run."""

    case_id: str = ""
    category: str = ""               # INTENT | DECOMPOSITION | …
    description: str = ""
    failure_type: FailureType = FailureType.UNKNOWN
    failure_reason: str = ""         # human-readable explanation

    input: str = ""                  # user_message
    expected: dict = Field(default_factory=dict)
    actual: dict = Field(default_factory=dict)

    trace_checks: list[dict] = Field(default_factory=list)
    # [{check: "intent.goal_category", expected: "CREATE_CONTENT", actual: "QUERY_INFO"}]

    # Phase 6.11 runtime regression snapshot fields.
    user_input: str = ""
    intent_spec: dict | None = None
    task_plan: dict | None = None
    execution_trace: object | None = None
    expected_behavior: dict = Field(default_factory=dict)


class BadCaseStore:
    """Small replaceable store for failed cases and regression snapshots."""

    def __init__(self) -> None:
        self._cases: list[BadCase] = []

    def save(self, case: BadCase) -> BadCase:
        self._cases.append(case.model_copy(deep=True))
        return case

    def list_cases(self) -> list[BadCase]:
        return [case.model_copy(deep=True) for case in self._cases]

    def clear(self) -> None:
        self._cases.clear()


__all__ = ["FailureType", "BadCase", "BadCaseStore"]
