"""Core Task domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


class TaskConfirmationState(StrEnum):
    """Task-level semantic confirmation lifecycle.

    Confirmation is a gate on the canonical Task, not on individual
    Objectives.  Runtime execution state remains in the existing execution
    repositories; this enum only answers whether the Task may enter work.
    """

    RESOLVED = "RESOLVED"
    AUTO_ADMITTED = "AUTO_ADMITTED"
    CONFIRMATION_PENDING = "CONFIRMATION_PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


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

    LEGACY_COMPATIBILITY: the new main path uses :class:`Objective`; this model
    is retained for the legacy fallback and cross-turn target resolution.
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


class ObjectiveStatus(StrEnum):
    """Lifecycle of one user intent inside a Task."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"
    WAITING = "WAITING"


class Objective(BaseModel):
    """The new main-path fact model: one user intent to be satisfied.

    Completion must come from a real Resource / Operation / Verification, never
    from an LLM claim.  ``expected_resource_kind`` is the verified resource that
    satisfies this objective (DRAFT, SCHEDULE, SEARCH_RESULT, POST, ...).

    ``result_requirement`` describes what actually satisfies the user intent so
    the loop does NOT treat "execution complete" as "objective complete":

    - DIRECT_RESULT:      the tool/resource itself IS the answer (query a status).
    - RESOURCE_MUTATION:  the goal is that a business resource changed; a
                          verified postcondition completes it (edit draft, cancel).
    - GROUNDED_SYNTHESIS: a NEW natural-language answer must be composed from
                          real current-Task evidence (search-then-summarize,
                          compare, analyze, explain).  A resource alone is NOT
                          completion; a composed FinalResult is.
    """

    model_config = ConfigDict(extra="forbid")

    objective_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    description: str = ""
    intent: str = ""
    status: ObjectiveStatus = ObjectiveStatus.PENDING
    expected_resource_kind: str = ""
    result_requirement: str = "DIRECT_RESULT"
    # Minimum distinct real sources a GROUNDED_SYNTHESIS Objective needs before
    # it may compose.  single-source synthesis=1; multi-source comparison=2.
    min_sources: int = 1
    # Per-Objective resolved constraints.  ``run_at`` here is the single time
    # authority: a canonical absolute instant from TemporalResolver (e.g.
    # "2026-08-17T02:00:00Z"), NOT a model guess.  ``timezone`` is the
    # display/business timezone.  This is per-objective so one turn can schedule
    # multiple targets at different times.
    constraints: dict[str, Any] = Field(default_factory=dict)
    # Objective-to-Objective prerequisites.  These are business relations,
    # independent from runtime step ids and durable execution state.
    dependencies: list[str] = Field(default_factory=list)
    # All capabilities a business Objective must satisfy.  A CommandItem may
    # require multiple (GENERATE_CONTENT + SCHEDULE_PUBLISH); the Objective is
    # only COMPLETED when EVERY required capability has a verified resource
    # (DRAFT AND SCHEDULE), not when the first one succeeds.
    required_capabilities: list[str] = Field(default_factory=list)
    expected_postcondition: dict[str, Any] = Field(default_factory=dict)
    related_resource_ids: list[str] = Field(default_factory=list)
    related_artifact_ids: list[str] = Field(default_factory=list)
    related_operations: list[str] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None


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
    # Immutable business ownership for multi-objective Tasks.  Legacy rows may
    # omit this field; new WRITE projections must always carry it.
    objective_id: str | None = None
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
    goal_category: str = ""           # CREATE_CONTENT | ANALYZE_CONTENT | …
    goal_summary: str | None = None

    # ── lifecycle ──
    status: TaskStatus = TaskStatus.CREATED
    phase: str | None = None
    priority: int = 0
    task_type: str = "GOAL_DRIVEN"
    execution_mode: str = "AUTO"

    # Task-level semantic confirmation.  The canonical facts themselves stay
    # in ``objectives`` / ``TaskDelta`` revisions / ResourceBinding.  These
    # fields only gate admission and correlate the existing durable Run that
    # resumes this Task after confirmation.
    requires_confirmation: bool = False
    confirmation_state: TaskConfirmationState = TaskConfirmationState.RESOLVED
    confirmation_version: int = 0
    confirmed_version: int | None = None
    confirmation_snapshot_hash: str | None = None
    confirmation_resume_run_id: str | None = None

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
    objectives: list[Objective] = Field(default_factory=list)
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
