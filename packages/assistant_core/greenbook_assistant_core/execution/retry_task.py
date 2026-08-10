"""Durable retry-task value objects shared by schedulers and stores."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class RetryTaskStatus(StrEnum):
    """Persistence lifecycle for a scheduled retry task."""

    READY = "READY"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RetryTask(BaseModel):
    """One delayed retry request, keyed by execution, step, and attempt."""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    step_id: str
    attempt: int = Field(ge=1)
    next_retry_time: datetime
    backoff: float = Field(default=0.0, ge=0.0)
    reason: str
    retry_budget: int = Field(default=1, ge=0)
    max_attempts: int = Field(default=1, ge=1)
    deadline: datetime | None = None
    operation_id: str | None = None
    status: RetryTaskStatus = RetryTaskStatus.READY
    claimed_by: str | None = None
    claim_until: datetime | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def key(self) -> tuple[str, str, int]:
        """Stable idempotency key for one logical step attempt."""

        return (self.execution_id, self.step_id, self.attempt)

    @property
    def task_id(self) -> str:
        """Bounded deterministic identifier used by durable stores."""

        material = f"greenbook:retry:{self.execution_id}:{self.step_id}:{self.attempt}"
        return f"retry-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


__all__ = ["RetryTask", "RetryTaskStatus"]
