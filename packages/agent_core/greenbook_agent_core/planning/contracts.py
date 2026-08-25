"""Canonical execution-plan contracts.

These models are the typed boundary between Goal compilation and Reliable
Execution.  They do not understand user language, select tools, or encode
fixed workflow templates.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PlanStep(BaseModel):
    """One resolved capability step in a task plan."""

    step_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    ordinal: int = 0
    capability: str = ""
    tool_name: str = ""
    description: str = ""
    depends_on: list[str] = Field(default_factory=list)
    input_artifact_types: list[str] = Field(default_factory=list)
    output_artifact_type: str = ""
    parallelizable: bool = False
    constraints: dict[str, Any] = Field(default_factory=dict)
    goal_id: str | None = None
    # Ephemeral progress in the disposable work plan; Objective/Execution are
    # the authoritative business and runtime state.
    status: str = "PENDING"


class TaskPlan(BaseModel):
    """Versioned executable plan for one durable Task."""

    plan_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    plan_source: str = "GOAL_COMPILER"
    plan_version: int = Field(default=1, ge=1)
    previous_plan_id: str | None = None
    change_reason: str = ""
    generated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class PlanRevision(BaseModel):
    """Immutable explanation for a versioned plan change."""

    revision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    plan_version: int = Field(ge=1)
    decision: str
    reason: str = ""
    observation: dict[str, Any] = Field(default_factory=dict)
    previous_plan_version: int | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class PlanningDecisionType(StrEnum):
    CONTINUE = "CONTINUE"
    INSERT_STEP = "INSERT_STEP"
    REMOVE = "REMOVE"
    REORDER = "REORDER"
    RETRY_WITH_NEW_ARGS = "RETRY_WITH_NEW_ARGS"
    SELECT_ALTERNATIVE_TOOL = "SELECT_ALTERNATIVE_TOOL"
    ASK_HUMAN = "ASK_HUMAN"
    FINISH = "FINISH"
    ABORT = "ABORT"


class PlanningDecision(BaseModel):
    """Typed output of DynamicPlanner for one runtime observation."""

    model_config = ConfigDict(extra="forbid")

    decision: PlanningDecisionType
    reason: str = ""
    task_id: str = ""
    goal_id: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    insert_nodes: list[Any] = Field(default_factory=list)
    remove_task_ids: list[str] = Field(default_factory=list)
    task_order: list[str] = Field(default_factory=list)
    retry_count: int = Field(default=0, ge=0)
    plan_version: int = Field(default=0, ge=0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("insert_nodes", mode="before")
    @classmethod
    def _coerce_insert_nodes(cls, value: Any) -> list[Any]:
        """Validate inserted nodes with the Goal-owned TaskNode model.

        The local import keeps the planning contract importable without the
        eager ``goal`` package initializer creating a compiler cycle.
        """

        from greenbook_agent_core.goal.models import TaskNode

        return [
            item if isinstance(item, TaskNode) else TaskNode.model_validate(item)
            for item in (value or [])
        ]


class MultiGoalPlan(BaseModel):
    """A compiled plan plus its durable goal projection."""

    task_id: str
    goals: list[Any] = Field(default_factory=list)
    plan: TaskPlan


__all__ = [
    "MultiGoalPlan",
    "PlanRevision",
    "PlanStep",
    "PlanningDecision",
    "PlanningDecisionType",
    "TaskPlan",
]
