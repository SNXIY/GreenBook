"""Memory models — MemoryType, MemoryRecord, MemoryQuery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class MemoryType(StrEnum):
    EPISODIC = "EPISODIC"
    SEMANTIC = "SEMANTIC"
    PROCEDURAL = "PROCEDURAL"


class MemoryRecord(BaseModel):
    """One memory entry."""

    memory_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    type: MemoryType = MemoryType.EPISODIC

    # ── content ──
    content: str = ""                           # human-readable summary
    embedding: list[float] | None = None        # vector (Phase 2)

    # ── metadata ──
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ── lifecycle ──
    importance: float = 0.5                     # 0.0–1.0
    access_count: int = 0
    last_accessed_at: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    expires_at: str | None = None               # TTL for low-importance


class MemoryQuery(BaseModel):
    """Search/filter parameters."""

    type: MemoryType | None = None
    user_id: str = ""
    keywords: list[str] = []
    metadata_filters: dict[str, Any] = Field(default_factory=dict)
    min_importance: float = 0.0
    limit: int = 10
    sort_by: str = "importance"    # importance | created_at | access_count
