"""Conservative, deterministic policy for writing long-term memory."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel


class MemoryWriteDecision(StrEnum):
    WRITE = "WRITE"
    SKIP = "SKIP"


class MemoryWritePolicy(BaseModel):
    """Allow only durable events with reusable user value."""

    minimum_importance: float = 0.35

    def evaluate(self, event: Mapping[str, Any]) -> MemoryWriteDecision:
        event_type = str(event.get("event_type", "")).upper()
        if event_type in {
            "TASK_COMPLETED",
            "TASK_FAILED_MAJOR",
            "USER_EXPLICIT_PREFERENCE",
            "USER_CORRECTION",
            "USER_REQUESTED_REMEMBER",
            "REUSABLE_STRATEGY",
        }:
            return MemoryWriteDecision.WRITE
        if event_type == "EXECUTION_OUTCOME" and (
            bool(event.get("major")) or bool(event.get("artifact_id"))
        ):
            return MemoryWriteDecision.WRITE
        return MemoryWriteDecision.SKIP

    def should_write(self, event: Mapping[str, Any]) -> bool:
        return self.evaluate(event) == MemoryWriteDecision.WRITE


__all__ = ["MemoryWriteDecision", "MemoryWritePolicy"]
