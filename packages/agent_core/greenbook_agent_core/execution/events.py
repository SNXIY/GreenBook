"""Immutable-ish events emitted by the execution runtime."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from greenbook_agent_core.observability.context import TraceContext


class EventType(StrEnum):
    EXECUTION_CREATED = "EXECUTION_CREATED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    STEP_STARTED = "STEP_STARTED"
    STEP_COMPLETED = "STEP_COMPLETED"
    STEP_FAILED = "STEP_FAILED"
    STEP_RETRY_REQUESTED = "STEP_RETRY_REQUESTED"
    STEP_RETRY_DENIED = "STEP_RETRY_DENIED"
    STEP_RETRY_STARTED = "STEP_RETRY_STARTED"
    STEP_RETRY_COMPLETED = "STEP_RETRY_COMPLETED"
    STEP_RETRY_EXHAUSTED = "STEP_RETRY_EXHAUSTED"
    STEP_RECONCILIATION_SUCCEEDED = "STEP_RECONCILIATION_SUCCEEDED"
    STEP_RECONCILIATION_FAILED = "STEP_RECONCILIATION_FAILED"
    EXECUTION_RECONCILIATION_REQUIRED = "EXECUTION_RECONCILIATION_REQUIRED"
    EXECUTION_PAUSE_REQUESTED = "EXECUTION_PAUSE_REQUESTED"
    EXECUTION_PAUSED = "EXECUTION_PAUSED"
    EXECUTION_RESUME_REQUESTED = "EXECUTION_RESUME_REQUESTED"
    EXECUTION_RESUMED = "EXECUTION_RESUMED"
    EXECUTION_CHECKPOINT_SAVED = "EXECUTION_CHECKPOINT_SAVED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    EXECUTION_WAITING_HUMAN = "EXECUTION_WAITING_HUMAN"
    EXECUTION_PLAN_REVISED = "EXECUTION_PLAN_REVISED"
    # Observability-only: one durable record per ActionLoop decision, keyed by
    # run_id.  Never read by the execution state machine.
    ACTION_LOOP_DECISION = "ACTION_LOOP_DECISION"
    EXECUTION_COMPLETED = "EXECUTION_COMPLETED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_CANCELLED = "EXECUTION_CANCELLED"


class ExecutionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str
    event_type: EventType
    step_id: str | None = None
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)
    trace_context: TraceContext | None = None


__all__ = ["EventType", "ExecutionEvent"]
