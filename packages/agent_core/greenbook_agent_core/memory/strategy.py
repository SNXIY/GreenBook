"""Retrieval of validated procedural strategy memories."""

from __future__ import annotations

from typing import Any

from .models import MemoryQuery, MemoryType


class StrategyRetriever:
    def __init__(self, repository: Any) -> None:
        self._repository = repository

    def retrieve(
        self,
        *,
        user_id: str = "",
        goal_category: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        records = self._repository.search(MemoryQuery(
            user_id=user_id,
            type=MemoryType.PROCEDURAL,
            limit=limit,
            sort_by="importance",
        ))
        values: list[dict[str, Any]] = []
        for record in records:
            metadata = record.metadata
            if metadata.get("success") is True:
                values.append({
                    "pattern": metadata.get("pattern", ""),
                    "plan_source": metadata.get("plan_source", ""),
                    "goal_category": metadata.get("goal_category", ""),
                    "confidence": metadata.get("confidence", 0.0),
                    "tool_count": metadata.get("tool_count", 0),
                    "description": record.content,
                })
        values.sort(key=lambda item: (
            0 if item["goal_category"] == goal_category else 1,
            -float(item["confidence"]),
        ))
        return values[:limit]

    def retrieve_best_plan_source(self, *, user_id: str = "", goal_category: str = "") -> str | None:
        values = self.retrieve(user_id=user_id, goal_category=goal_category, limit=3)
        return values[0].get("plan_source") if values else None


__all__ = ["StrategyRetriever"]
