"""Planning domain models — ExecutablePlan, ValidationError."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .contracts import PlanStep


class ValidationError(BaseModel):
    """A single validation failure on a PlanStep."""

    step_id: str = ""
    ordinal: int = 0
    capability: str = ""
    error_code: str = ""            # UNKNOWN_CAPABILITY | MISSING_TOOL | CYCLIC_DEP | …
    message: str = ""


class ExecutablePlan(BaseModel):
    """A TaskPlan that has passed (or partially failed) pre-execution validation.

    When ``is_valid`` is True the plan is ready for execution (Phase 4).
    When False, ``errors`` contains every validation failure found.
    """

    plan_id: str = ""
    task_id: str = ""
    plan_source: str = ""
    plan_version: int = Field(default=1, ge=1)

    steps: list[PlanStep] = []
    errors: list[ValidationError] = []

    # ── execution readiness ──
    is_valid: bool = False
    requires_approval: bool = False          # any step needs user approval?
    has_side_effects: bool = False            # any step modifies external state?
    capabilities_validated: bool = False
    tools_mapped: bool = False
    dependencies_checked: bool = False
    artifacts_checked: bool = False
    cycles_checked: bool = False

    validated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    @property
    def error_count(self) -> int:
        return len(self.errors)

    def add_error(self, step: PlanStep, code: str, message: str) -> None:
        self.errors.append(ValidationError(
            step_id=step.step_id,
            ordinal=step.ordinal,
            capability=step.capability,
            error_code=code,
            message=message,
        ))
