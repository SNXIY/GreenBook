"""Artifact domain model — persisted output of a capability step."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ArtifactLifecycle(StrEnum):
    CREATED = "CREATED"
    AVAILABLE = "AVAILABLE"
    CONSUMED = "CONSUMED"
    ARCHIVED = "ARCHIVED"
    FAILED = "FAILED"


class ArtifactReference(BaseModel):
    """Small cross-agent handle; never carries the artifact body."""

    artifact_id: str
    artifact_type: str
    owner_task_id: str = ""
    owner_execution_id: str = ""
    created_by_agent: str = ""
    metadata_schema: str = Field(default="", alias="schema")
    version: int = 1
    storage_type: str = "INLINE"
    location: str | None = None
    content_hash: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class Artifact(BaseModel):
    """An immutable record produced by a completed capability step.

    Artifacts flow between steps in a TaskPlan: Step 1 produces
    SEARCH_RESULT, Step 2 consumes it to produce ANALYSIS_REPORT, etc.
    """

    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    execution_id: str = ""
    owner_task_id: str = ""
    owner_execution_id: str = ""
    created_by_agent: str = ""
    step_id: str = ""                  # PlanStep.step_id that produced this

    # ── type ──
    artifact_type: str = ""            # SEARCH_RESULT | DRAFT | ANALYSIS_REPORT | …
    resource_id: str | None = None     # external id (draft_id, schedule_id, …)
    resource_kind: str | None = None   # DRAFT | POST | SCHEDULE
    resource_type: str | None = None   # durable presentation-facing resource type

    # ── content ──
    title: str | None = None
    summary: str = ""
    status: str | None = None
    run_at: str | None = None
    timezone: str | None = None
    metadata_schema: str = ""
    size: int | None = None
    version: int = 1
    storage_type: str = "INLINE"
    location: str | None = None
    content_hash: str | None = None
    lifecycle: ArtifactLifecycle = ArtifactLifecycle.CREATED
    consumed_by_task_ids: list[str] = []
    metadata: dict[str, Any] = {}      # flexible payload (tool result data, …)

    # ── provenance ──
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_reference(self) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=self.artifact_id,
            artifact_type=self.artifact_type,
            owner_task_id=self.owner_task_id or self.task_id,
            owner_execution_id=self.owner_execution_id or self.execution_id,
            created_by_agent=self.created_by_agent,
            schema=self.metadata_schema,
            version=self.version,
            storage_type=self.storage_type,
            location=self.location,
            content_hash=self.content_hash,
        )
