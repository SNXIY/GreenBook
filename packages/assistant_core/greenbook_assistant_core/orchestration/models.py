"""Orchestration domain models — TaskPlan, PlanStep, PlanTemplate."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    """A single step in a TaskPlan."""

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ordinal: int = 0
    capability: str = ""                    # SEARCH_COMMUNITY, GENERATE_CONTENT, …
    description: str = ""

    # ── DAG edges ──
    depends_on: list[str] = []              # step_ids that must complete first

    # ── artifact flow ──
    input_artifact_types: list[str] = []    # SEARCH_RESULT, DRAFT, …
    output_artifact_type: str = ""          # SEARCH_RESULT, ANALYSIS_REPORT, …

    # ── execution hints ──
    parallelizable: bool = False
    constraints: dict[str, Any] = {}


class TaskPlan(BaseModel):
    """A validated execution plan for one Task."""

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""

    steps: list[PlanStep] = []

    # ── metadata ──
    template_name: str = ""                 # which template produced this plan
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class PlanTemplate(BaseModel):
    """A reusable recipe for building TaskPlans.

    A template defines the *shape* of a plan — capability names, DAG
    edges, and artifact flow — without pinning specific tool arguments.
    The orchestrator instantiates a template by filling in step
    descriptions and constraints from the TaskIntent.
    """

    name: str                                          # "CREATE_AND_PUBLISH"
    description: str
    steps: list[PlanStep] = []

    def instantiate(self, task_id: str) -> TaskPlan:
        """Produce a concrete TaskPlan from this template."""
        plan = TaskPlan(task_id=task_id, template_name=self.name)
        for i, s in enumerate(self.steps):
            step = s.model_copy(deep=True)
            step.step_id = str(uuid.uuid4())
            step.ordinal = i + 1
            plan.steps.append(step)
        return plan
