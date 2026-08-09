"""Artifact domain model — persisted output of a capability step."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Artifact(BaseModel):
    """An immutable record produced by a completed capability step.

    Artifacts flow between steps in a TaskPlan: Step 1 produces
    SEARCH_RESULT, Step 2 consumes it to produce ANALYSIS_REPORT, etc.
    """

    artifact_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    execution_id: str = ""
    step_id: str = ""                  # PlanStep.step_id that produced this

    # ── type ──
    artifact_type: str = ""            # SEARCH_RESULT | DRAFT | ANALYSIS_REPORT | …
    resource_id: str | None = None     # external id (draft_id, schedule_id, …)
    resource_kind: str | None = None   # DRAFT | POST | SCHEDULE

    # ── content ──
    summary: str = ""
    metadata: dict[str, Any] = {}      # flexible payload (tool result data, …)

    # ── provenance ──
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
