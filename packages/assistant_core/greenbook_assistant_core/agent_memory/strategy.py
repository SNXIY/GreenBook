"""StrategyRetriever — recall relevant procedural strategies.

Phase 6.6 Stage 4: retrieves strategies from MemoryStore.
"""

from __future__ import annotations

from typing import Any

from .models import MemoryQuery, MemoryType
from .store import MemoryStore


class StrategyRetriever:
    """Retrieve relevant procedural strategies for an execution context."""

    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    # ── main entry ───────────────────────────────────────────────

    def retrieve(
        self,
        *,
        user_id: str = "",
        goal_category: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Retrieve successful strategies for a goal_category.

        Returns strategies ordered by confidence desc, filtered
        to only include successful patterns.
        """
        query = MemoryQuery(
            user_id=user_id,
            type=MemoryType.PROCEDURAL,
            limit=limit,
            sort_by="importance",
        )
        records = self._store.search(query)

        # Filter: only successful, relevant to goal_category
        results: list[dict[str, Any]] = []
        for r in records:
            meta = r.metadata
            # Prefer matching goal_category, but include all
            if meta.get("success") is True:
                results.append({
                    "pattern": meta.get("pattern", ""),
                    "template": meta.get("template", ""),
                    "goal_category": meta.get("goal_category", ""),
                    "confidence": meta.get("confidence", 0.0),
                    "tool_count": meta.get("tool_count", 0),
                    "description": r.content,
                })

        # Sort: exact category match first, then by confidence
        results.sort(
            key=lambda s: (
                0 if s["goal_category"] == goal_category else 1,
                -s["confidence"],
            )
        )
        return results[:limit]

    def retrieve_best_template(
        self,
        *,
        user_id: str = "",
        goal_category: str = "",
    ) -> str | None:
        """Return the best-known template name for a goal_category."""
        strategies = self.retrieve(
            user_id=user_id, goal_category=goal_category, limit=3,
        )
        if not strategies:
            return None
        # Return the most confident template
        return strategies[0].get("template") or None
