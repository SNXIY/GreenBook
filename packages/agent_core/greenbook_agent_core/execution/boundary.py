"""Execution boundary tracking for safe runtime fallback.

The boundary records whether a turn has crossed from pure reasoning/reads into
durable side effects.  Once any write operation is submitted — or a
RESULT_UNKNOWN is observed — the turn must not automatically re-plan and
re-execute through a fallback Runtime, because that would risk duplicate side
effects.  The boundary is per-turn and never shared across concurrent turns.
"""

from __future__ import annotations

from typing import Any


class TurnExecutionBoundary:
    """Per-turn tracker of whether an irreversible operation has started."""

    def __init__(self) -> None:
        self.side_effect_started: bool = False
        self.operation_submitted: bool = False
        self.result_unknown: bool = False
        self.submitted_operations: int = 0
        self.submitted_tools: list[str] = []

    @property
    def phase(self) -> str:
        return "AFTER_SIDE_EFFECT" if self.side_effect_started else "BEFORE_SIDE_EFFECT"

    def record_operation_submitted(self, tool_name: str = "") -> None:
        """Mark that a durable write has been submitted (irreversible)."""
        self.side_effect_started = True
        self.operation_submitted = True
        self.submitted_operations += 1
        if tool_name and tool_name not in self.submitted_tools:
            self.submitted_tools.append(tool_name)

    def record_result_unknown(self) -> None:
        """Mark that a result could not be resolved to a terminal state."""
        self.side_effect_started = True
        self.result_unknown = True

    def record_read(self) -> None:
        """Reads are re-runnable and never block fallback."""
        return None

    def can_fallback(self) -> bool:
        """A turn may only fall back before any side effect has started."""
        return not self.side_effect_started

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "side_effect_started": self.side_effect_started,
            "operation_submitted": self.operation_submitted,
            "result_unknown": self.result_unknown,
            "submitted_operations": self.submitted_operations,
            "submitted_tools": list(self.submitted_tools),
        }


__all__ = ["TurnExecutionBoundary"]
