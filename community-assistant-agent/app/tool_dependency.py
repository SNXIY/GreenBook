"""Shared dependency contracts for async tools (e.g. Creator durable tasks)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DependencyStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ToolDependencyDescriptor(BaseModel):
    """Durable remote dependency identity. Never stores tokens or full body text."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "creator"
    dependency_type: str = "CREATOR_TASK"
    remote_task_id: str
    tool_name: str

    run_id: str
    step_id: str | None = None
    side_effect_id: str | None = None
    operation_key: str

    status: DependencyStatus = DependencyStatus.PENDING
    poll_after: datetime | None = None
    deadline_at: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    def safe_public_dict(self) -> dict[str, Any]:
        meta = dict(self.metadata or {})
        # Keep only non-sensitive display fields.
        allowed = {
            "required_action",
            "display_message",
            "interrupt_id",
            "checkpoint_id",
            "pending_decision_id",
            "submitted_at",
            "operation_mode",
            "source_draft_id",
            "expected_content_sha256",
            "idempotency_recovery",
            "poll_count",
        }
        return {
            "provider": self.provider,
            "dependency_type": self.dependency_type,
            "remote_task_id": self.remote_task_id,
            "tool_name": self.tool_name,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "side_effect_id": self.side_effect_id,
            "operation_key": self.operation_key,
            "status": self.status.value,
            "poll_after": self.poll_after.isoformat() if self.poll_after else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
            "metadata": {k: meta[k] for k in allowed if k in meta},
        }


class DependencyPending(Exception):
    """Control-flow: tool step is waiting on a remote dependency."""

    def __init__(
        self,
        *,
        task_id: str,
        status: str,
        state: dict[str, Any],
        dependency_type: str = "CREATOR_TASK",
        descriptor: ToolDependencyDescriptor | None = None,
    ) -> None:
        super().__init__(f"{dependency_type} {task_id} is {status}")
        self.task_id = task_id
        self.status = status
        self.state = state
        self.dependency_type = dependency_type
        self.descriptor = descriptor


async def resume_creator_dependency(
    *,
    task_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Forward-compatible stub for Creator HITL resume (not wired to UX yet)."""

    del payload
    return {
        "task_id": task_id,
        "status": "NOT_IMPLEMENTED",
        "message": "Creator HITL resume is reserved for a later phase",
    }
