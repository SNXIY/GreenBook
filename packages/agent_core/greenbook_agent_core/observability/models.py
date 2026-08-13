"""Trace and TraceEvent domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .context import TraceContext


class EventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    PLAN_CREATED = "PLAN_CREATED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    STEP_STARTED = "STEP_STARTED"
    TOOL_INVOKED = "TOOL_INVOKED"
    TOOL_COMPLETED = "TOOL_COMPLETED"
    TOOL_FAILED = "TOOL_FAILED"
    ARTIFACT_CREATED = "ARTIFACT_CREATED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"

    # Phase 6.2 — group-level events
    GROUP_CREATED = "GROUP_CREATED"
    SUB_TASK_STARTED = "SUB_TASK_STARTED"
    SUB_TASK_COMPLETED = "SUB_TASK_COMPLETED"
    SUB_TASK_FAILED = "SUB_TASK_FAILED"
    SUB_TASK_SKIPPED = "SUB_TASK_SKIPPED"
    GROUP_COMPLETED = "GROUP_COMPLETED"

    # Phase 6.4 — parallel execution events
    GROUP_PARALLEL_STARTED = "GROUP_PARALLEL_STARTED"
    SUB_TASK_BATCH_STARTED = "SUB_TASK_BATCH_STARTED"
    GROUP_PARALLEL_COMPLETED = "GROUP_PARALLEL_COMPLETED"


class TraceEvent(BaseModel):
    """A single event in an execution trace."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = ""
    event_type: EventType

    # ── context ──
    execution_id: str = ""
    step_id: str = ""
    tool_name: str = ""
    capability: str = ""
    artifact_type: str = ""

    # ── payload ──
    payload: dict[str, Any] = {}
    trace_context: TraceContext | None = None

    # ── timing ──
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class Trace(BaseModel):
    """Root object for one execution — groups all events."""

    trace_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    execution_id: str = ""
    user_id: str = ""
    events: list[TraceEvent] = []
    trace_context: TraceContext | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str = ""

    @property
    def event_count(self) -> int:
        return len(self.events)

    def timeline(self) -> list[TraceEvent]:
        """Events ordered by timestamp ascending."""
        return sorted(self.events, key=lambda e: e.timestamp)
