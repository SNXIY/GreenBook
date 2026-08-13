"""Execution state models — PlanExecution, StepExecution, ExecutionStatus."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"                        # user-controlled pause
    WAITING_APPROVAL = "WAITING_APPROVAL"    # legacy — kept for compat
    WAITING_HUMAN = "WAITING_HUMAN"          # Phase 6.5 — unified pause
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionControlState(StrEnum):
    """Durable human-control state, separate from execution lifecycle status."""

    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESUMING = "RESUMING"
    CANCELLED = "CANCELLED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class ArtifactHandle(BaseModel):
    """Reference to an artifact consumed or produced by a step."""
    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    artifact_type: str = ""            # SEARCH_RESULT | DRAFT | ANALYSIS_REPORT | …
    resource_id: str | None = None     # external id (draft_id, …)
    summary: str | None = None
    # Body-free identifiers that downstream contracts may bind from a
    # collection artifact (for example SEARCH_RESULT -> post_id).
    resource_refs: list[dict[str, Any]] = Field(default_factory=list)


class StepExecution(BaseModel):
    """Runtime state for one PlanStep."""

    step_execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str = ""
    step_id: str = ""                  # links to PlanStep.step_id
    capability: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = {}
    idempotency_key: str = ""
    execution_mode: str = "QUEUE"
    policy_snapshot: dict[str, Any] = {}
    ordinal: int = 0

    # ── plan-level type info (carried from PlanStep) ──
    input_artifact_types: list[str] = []
    output_artifact_type: str = ""
    depends_on: list[str] = []

    # ── status ──
    status: StepStatus = StepStatus.PENDING
    retry_count: int = 0
    max_retries: int = 3

    # ── error ──
    error_code: str = ""
    error_message: str = ""

    # ── artifacts ──
    input_artifacts: list[ArtifactHandle] = []
    output_artifact: ArtifactHandle | None = None

    # ── timing ──
    started_at: str = ""
    completed_at: str = ""

    # ── checkpoint ──
    checkpoint_data: dict[str, Any] = {}
    version: int = 1


class PlanExecution(BaseModel):
    """Runtime wrapper for a TaskPlan — tracks overall and per-step state."""

    execution_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    plan_id: str = ""
    task_id: str = ""

    # ── overall status ──
    status: ExecutionStatus = ExecutionStatus.PENDING
    control_state: ExecutionControlState = ExecutionControlState.RUNNING
    control_reason: str = ""
    control_requested_at: str = ""
    control_updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    # ── steps ──
    steps: list[StepExecution] = []
    current_step_index: int = 0

    # ── approval ──
    requires_approval: bool = False
    has_side_effects: bool = False

    # ── timing ──
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str = ""

    # ── checkpoint ──
    version: int = 1

    # ── computed ──

    @property
    def completed_step_count(self) -> int:
        return sum(1 for s in self.steps if s.status == StepStatus.COMPLETED)

    @property
    def total_step_count(self) -> int:
        return len(self.steps)

    @property
    def failed_step_count(self) -> int:
        return sum(1 for s in self.steps
                   if s.status in (StepStatus.FAILED, StepStatus.FAILED_RETRYABLE))

    @property
    def next_pending_step(self) -> StepExecution | None:
        for s in sorted(self.steps, key=lambda x: x.ordinal):
            if s.status == StepStatus.PENDING:
                return s
        return None

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            ExecutionStatus.COMPLETED,
            ExecutionStatus.FAILED,
            ExecutionStatus.CANCELLED,
        )
