"""ProceduralMemoryExtractor — distill execution experience into patterns.

Phase 6.6 Stage 4: analyzes execution outcomes and produces
PROCEDURAL MemoryRecords.
"""

from __future__ import annotations

from typing import Any

from .models import MemoryRecord, MemoryType


class ProceduralMemoryExtractor:
    """Extract strategy patterns from execution results."""

    # ── main entry ───────────────────────────────────────────────

    @staticmethod
    def extract(
        *,
        user_id: str,
        goal_category: str = "",
        template_name: str = "",
        status: str = "",
        tool_count: int = 0,
        step_count: int = 0,
        error_code: str = "",
    ) -> MemoryRecord | None:
        """Analyze one execution and produce a procedural memory.

        Returns None when there's nothing interesting to remember
        (simple single-step success with no novelty).
        """

        # Only record for non-trivial executions
        if step_count <= 1 and status == "COMPLETED":
            return None  # Too simple to learn from

        success = status == "COMPLETED"

        pattern = ProceduralMemoryExtractor._derive_pattern(
            goal_category=goal_category,
            template_name=template_name,
            success=success,
            tool_count=tool_count,
            error_code=error_code,
        )

        return MemoryRecord(
            user_id=user_id,
            type=MemoryType.PROCEDURAL,
            content=pattern["description"],
            metadata={
                "pattern": pattern["key"],
                "goal_category": goal_category,
                "template": template_name,
                "success": success,
                "tool_count": tool_count,
                "step_count": step_count,
                "error_code": error_code,
                "confidence": pattern["confidence"],
            },
            importance=pattern["importance"],
        )

    # ── pattern derivation ───────────────────────────────────────

    @staticmethod
    def _derive_pattern(
        goal_category: str,
        template_name: str,
        success: bool,
        tool_count: int,
        error_code: str,
    ) -> dict[str, Any]:
        """Derive a named pattern from execution details."""
        key = f"{goal_category}:{template_name}"
        if success:
            desc = (
                f"[{goal_category}] Template '{template_name}' succeeded "
                f"with {tool_count} tool calls"
            )
            confidence = min(0.5 + tool_count * 0.1, 0.9)
            importance = 0.4
        else:
            reason = error_code or "unknown"
            desc = (
                f"[{goal_category}] Template '{template_name}' FAILED "
                f"(reason: {reason})"
            )
            confidence = 0.2
            importance = 0.3

        return {
            "key": key,
            "description": desc,
            "confidence": confidence,
            "importance": importance,
        }
