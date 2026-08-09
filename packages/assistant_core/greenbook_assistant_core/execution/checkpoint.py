"""Non-authoritative execution checkpoint snapshots."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExecutionCheckpoint(BaseModel):
    execution_id: str
    completed_steps: list[str] = Field(default_factory=list)
    current_step: str = ""
    snapshot: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ExecutionCheckpoint"]
