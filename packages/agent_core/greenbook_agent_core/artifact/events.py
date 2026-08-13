"""Auditable Artifact lifecycle events."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ArtifactEventType(StrEnum):
    CREATED = "ARTIFACT_CREATED"
    AVAILABLE = "ARTIFACT_AVAILABLE"
    CONSUMED = "ARTIFACT_CONSUMED"
    ARCHIVED = "ARTIFACT_ARCHIVED"
    FAILED = "ARTIFACT_FAILED"


class ArtifactLifecycleEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    artifact_id: str
    artifact_type: str
    execution_id: str = ""
    task_id: str = ""
    agent_name: str = ""
    event_type: ArtifactEventType
    lifecycle: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ArtifactEventType", "ArtifactLifecycleEvent"]
