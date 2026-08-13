"""Typed user-correction event at the Command/Memory boundary."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class CorrectionEvent(BaseModel):
    """A resolved correction, not an unstructured chat log."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    conversation_id: str = ""
    task_id: str | None = None
    original_target: dict[str, Any] = Field(default_factory=dict)
    corrected_target: dict[str, Any] = Field(default_factory=dict)
    correction_summary: str = ""
    preference_candidate: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


__all__ = ["CorrectionEvent"]
