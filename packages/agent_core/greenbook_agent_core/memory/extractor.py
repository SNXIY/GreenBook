"""Distill non-trivial execution outcomes into procedural memories."""

from __future__ import annotations

from typing import Any

from .models import MemoryRecord, MemoryType


class ProceduralMemoryExtractor:
    @staticmethod
    def extract(
        *,
        user_id: str,
        goal_category: str = "",
        plan_source: str = "",
        status: str = "",
        tool_count: int = 0,
        step_count: int = 0,
        error_code: str = "",
    ) -> MemoryRecord | None:
        if step_count <= 1 and status == "COMPLETED":
            return None
        success = status == "COMPLETED"
        pattern = ProceduralMemoryExtractor._derive_pattern(
            goal_category, plan_source, success, tool_count, error_code
        )
        return MemoryRecord(
            user_id=user_id,
            memory_type=MemoryType.PROCEDURAL,
            content=pattern["description"],
            structured_metadata={
                "pattern": pattern["key"],
                "goal_category": goal_category,
                "plan_source": plan_source,
                "success": success,
                "tool_count": tool_count,
                "step_count": step_count,
                "error_code": error_code,
                "confidence": pattern["confidence"],
            },
            importance=pattern["importance"],
        )

    @staticmethod
    def _derive_pattern(
        goal_category: str,
        plan_source: str,
        success: bool,
        tool_count: int,
        error_code: str,
    ) -> dict[str, Any]:
        key = f"{goal_category}:{plan_source}"
        if success:
            description = f"[{goal_category}] resolved execution succeeded with {tool_count} tool calls"
            return {"key": key, "description": description, "confidence": min(0.5 + tool_count * 0.1, 0.9), "importance": 0.4}
        reason = error_code or "unknown"
        return {
            "key": key,
            "description": f"[{goal_category}] resolved execution failed (reason: {reason})",
            "confidence": 0.2,
            "importance": 0.3,
        }


__all__ = ["ProceduralMemoryExtractor"]
