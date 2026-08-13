"""Core Task domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from greenbook_agent_core.planning.contracts import PlanRevision


class TaskStatus(StrEnum):
    """Canonical durable Task lifecycle.

    ``IN_PROGRESS`` remains readable for the older execution projection;
    new lifecycle mutations use ``RUNNING``.
    """

    CREATED = "CREATED"
    PLANNING = "PLANNING"
    READY = "READY"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    PAUSED = "PAUSED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ── resolved target — Phase 2 ────────────────────────────────────────────

class ResolvedTaskTarget(BaseModel):
    """Result of resolving a resolved target's target reference to a concrete Task."""

    task_id: str
    goal: str = ""
    goal_category: str = ""
    confidence: float = 0.0            # 0.0–1.0
    match_reason: str = ""             # human-readable: "exact_id", "label_match", …
    match_level: int = 0               # 1=exact_id, 2=label, 3=artifact, 4=category, 5=recent
    candidates: list[str] = []         # alternative task_ids when ambiguous
    is_ambiguous: bool = False         # True when multiple valid targets exist


# ── Artifact / Task — Phase 1 ───────────────────────────────────────

class ArtifactRef(BaseModel):
    """Lightweight reference to a piece of data produced during a run."""
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    step_id: str = ""                 # Phase 3+ — filled by Execution Engine
    artifact_type: str = ""           # DRAFT | SEARCH_RESULT | SCHEDULE
    resource_id: str | None = None    # external id (draft_id, schedule_id, …)
    resource_kind: str | None = None  # DRAFT | POST | SCHEDULE
    summary: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TaskGoal(BaseModel):
    """A durable goal inside a Task.

    ``Task`` remains the long-lived business objective and ``Execution`` stays
    the runtime instance.  This small projection lets the conversation layer
    track sub-goals without changing execution state.
    """

    goal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    description: str = ""
    kind: str = ""
    status: str = "PENDING"
    depends_on_goal_ids: list[str] = []
    artifact_refs: list[ArtifactRef] = []
    execution_id: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TaskRevisionType(StrEnum):
    ADD_GOAL = "ADD_GOAL"
    MODIFY_GOAL = "MODIFY_GOAL"
    REPLAN = "REPLAN"


class TaskRevision(BaseModel):
    """Auditable change applied to a long-lived Task."""

    revision_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    type: TaskRevisionType
    payload: dict[str, Any] = Field(default_factory=dict)
    previous_version: int = 0
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TaskExecutionRef(BaseModel):
    """A read-model link from a Task/Goal to a Runtime execution."""

    execution_id: str
    task_id: str
    goal_id: str | None = None
    status: str = "PENDING"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TaskResourceRef(BaseModel):
    """Structured resource index used by cross-turn target resolution."""

    resource_id: str
    resource_kind: str = ""
    title: str | None = None
    status: str | None = None
    scheduled_at: str | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Task(BaseModel):
    """A long-running user goal that may span multiple turns."""

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    user_id: str
    tenant_id: str

    # ── goal ──
    goal: str = ""
    goal_category: str = ""           # CREATE_CONTENT | IMPROVE_CONTENT | …
    goal_summary: str | None = None

    # ── lifecycle ──
    status: TaskStatus = TaskStatus.CREATED
    phase: str | None = None
    priority: int = 0
    task_type: str = "GOAL_DRIVEN"
    execution_mode: str = "AUTO"

    # Canonical Goal/Plan bindings.  Snapshots are projections; execution
    # truth remains in the reliable execution repositories.
    root_goal_id: str | None = None
    goal_tree_version: int = 0
    goal_tree_snapshot: dict[str, Any] = Field(default_factory=dict)
    plan_version: int = 0
    plan_history: list[PlanRevision] = Field(default_factory=list)
    active_execution_id: str | None = None

    # ── data ──
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)        # task_ids this task depends on
    goals: list[TaskGoal] = Field(default_factory=list)
    revisions: list[TaskRevision] = Field(default_factory=list)
    execution_refs: list[TaskExecutionRef] = Field(default_factory=list)
    resource_index: list[TaskResourceRef] = Field(default_factory=list)
    last_action: str | None = None
    action_history: list[str] = Field(default_factory=list)

    # ── tracking ──
    last_error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    version: int = 1

    # ── timestamps ──
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
