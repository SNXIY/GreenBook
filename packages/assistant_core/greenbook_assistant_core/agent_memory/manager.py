"""MemoryManager — high-level memory operations.

Phase 1: remember, recall, forget.  Phase 2+: integrate with Runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .models import MemoryQuery, MemoryRecord, MemoryType
from .store import MemoryStore


class MemoryManager:
    """Business logic for Agent Memory."""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self._store = store or MemoryStore()

    # ── core API ────────────────────────────────────────────────

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        """Save a new memory."""
        return self._store.save(record)

    def recall(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Search memories. Updates access_count on results."""
        results = self._store.search(query)
        now = datetime.now(UTC).isoformat()
        for r in results:
            r.access_count += 1
            r.last_accessed_at = now
        return results

    def forget(self, memory_id: str) -> None:
        """Delete a memory."""
        self._store.delete(memory_id)

    # ── episodic ────────────────────────────────────────────────

    def remember_execution(
        self,
        user_id: str,
        goal: str = "",
        category: str = "",
        status: str = "",
        draft_id: str | None = None,
        schedule_id: str | None = None,
        **extra: Any,
    ) -> MemoryRecord:
        """Record a completed execution as episodic memory."""
        return self.remember(MemoryRecord(
            user_id=user_id,
            type=MemoryType.EPISODIC,
            content=f"[{status}] {goal}",
            metadata={
                "goal": goal,
                "goal_category": category,
                "status": status,
                "draft_id": draft_id,
                "schedule_id": schedule_id,
                **extra,
            },
            importance=self._exec_importance(status, bool(draft_id)),
        ))

    # ── semantic ────────────────────────────────────────────────

    def remember_preference(
        self,
        user_id: str,
        preference_type: str,
        value: str,
        confidence: float = 0.5,
    ) -> MemoryRecord:
        """Store a user preference."""
        return self.remember(MemoryRecord(
            user_id=user_id,
            type=MemoryType.SEMANTIC,
            content=f"Prefers {preference_type}: {value}",
            metadata={
                "preference_type": preference_type,
                "value": value,
                "confidence": confidence,
            },
            importance=min(confidence * 0.8, 0.9),
        ))

    # ── procedural ──────────────────────────────────────────────

    def remember_pattern(
        self,
        user_id: str,
        pattern: str,
        success: bool = True,
        context: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        """Store an execution pattern observation."""
        return self.remember(MemoryRecord(
            user_id=user_id,
            type=MemoryType.PROCEDURAL,
            content=pattern,
            metadata={
                "pattern_type": pattern,
                "success": success,
                "context": context or {},
            },
            importance=0.3,
        ))

    # ── helpers ─────────────────────────────────────────────────

    @staticmethod
    def _exec_importance(status: str, has_side_effect: bool) -> float:
        base = 0.5
        if status == "COMPLETED":
            base += 0.2
        if has_side_effect:
            base += 0.2
        return min(base, 1.0)

    # ── queries ─────────────────────────────────────────────────

    @property
    def store(self) -> MemoryStore:
        return self._store
