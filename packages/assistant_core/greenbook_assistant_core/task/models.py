"""Core Task domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ── TaskIntent — Phase 2 ────────────────────────────────────────────

TaskRelation = Literal[
    "NEW_TASK",
    "CONTINUE_TASK",
    "MODIFY_TASK",
    "QUERY_TASK",
    "CANCEL_TASK",
    "DIRECT",
]

GoalCategory = Literal[
    "CREATE_CONTENT",
    "IMPROVE_CONTENT",
    "ANALYZE_COMMUNITY",
    "PUBLISH_CONTENT",
    "MANAGE_SCHEDULE",
    "INTERACT",
    "QUERY_INFO",
    "COMPOSITE",
]


class EntityHint(BaseModel):
    """A reference the user made to a known entity."""
    kind: str = ""                    # DRAFT | POST | SCHEDULE | TASK | ARTIFACT
    label: str | None = None          # user-facing description
    entity_id: str | None = None      # explicit ID if user provided one


class TaskIntent(BaseModel):
    """Structured understanding of one user turn — Phase 2 output."""

    # ── relationship to existing tasks ──
    relation: TaskRelation = "NEW_TASK"

    # ── goal ──
    goal: str = ""                    # one-sentence distillation
    goal_category: GoalCategory | str = "QUERY_INFO"

    # ── target (when relation != NEW_TASK) ──
    target_task_id: str | None = None
    target_task_hint: str | None = None    # "刚才那篇", "Java文章"
    target_entity_refs: list[EntityHint] = []

    # ── structured needs (Phase 3+ consumed by Planner) ──
    requirements: list[dict[str, Any]] = []  # [{type:"SEARCH",params:{...}},...]
    constraints: list[dict[str, Any]] = []   # [{type:"TIME",value:"..."},...]

    # ── resource requests — Phase 5.6 ──
    resource_requests: list[dict[str, str]] = []
    # [{operation:"CREATE", resource_type:"CONTENT_DRAFT"}, …]

    # ── confidence ──
    confidence: float = 0.0
    source: Literal["L1", "L2"] = "L1"  # which layer produced this

    # ── Phase 6.8.1: raw IntentSpec snapshot (for downstream that opts in) ──
    intent_spec: dict[str, object] | None = None


# ── ResolvedTaskTarget — Phase 2.5 ───────────────────────────────────

class ResolvedTaskTarget(BaseModel):
    """Result of resolving a TaskIntent's target reference to a concrete Task."""

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
    status: TaskStatus = TaskStatus.READY
    phase: str | None = None

    # ── data ──
    artifacts: list[ArtifactRef] = []
    depends_on: list[str] = []        # task_ids this task depends on
    goals: list[TaskGoal] = []
    execution_refs: list[TaskExecutionRef] = []
    resource_index: list[TaskResourceRef] = []
    last_action: str | None = None
    action_history: list[str] = []

    # ── tracking ──
    last_error: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    version: int = 1

    # ── timestamps ──
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None
