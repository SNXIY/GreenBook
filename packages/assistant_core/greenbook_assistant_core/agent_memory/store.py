"""MemoryStore — in-memory CRUD for MemoryRecords.

Phase 1: in-memory.  Phase 2+: PostgreSQL + vector search.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .models import MemoryQuery, MemoryRecord


class MemoryStore:
    """In-memory store for MemoryRecords."""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    # ── CRUD ──

    def save(self, record: MemoryRecord) -> MemoryRecord:
        self._records[record.memory_id] = record
        return record

    def find_by_id(self, memory_id: str) -> MemoryRecord | None:
        return self._records.get(memory_id)

    def search(self, query: MemoryQuery) -> list[MemoryRecord]:
        results = list(self._records.values())

        # Filter by user
        if query.user_id:
            results = [r for r in results if r.user_id == query.user_id]

        # Filter by type
        if query.type:
            results = [r for r in results if r.type == query.type]

        # Filter by importance
        if query.min_importance > 0:
            results = [r for r in results
                       if r.importance >= query.min_importance]

        # Filter by metadata
        if query.metadata_filters:
            for key, val in query.metadata_filters.items():
                results = [r for r in results
                           if r.metadata.get(key) == val]

        # Keyword search
        if query.keywords:
            kws = [k.lower() for k in query.keywords]
            results = [r for r in results
                       if any(kw in r.content.lower() for kw in kws)]

        # Filter expired
        results = [r for r in results if not self._is_expired(r)]

        # Sort
        if query.sort_by == "created_at":
            results.sort(key=lambda r: r.created_at, reverse=True)
        elif query.sort_by == "access_count":
            results.sort(key=lambda r: r.access_count, reverse=True)
        else:  # importance (default)
            results.sort(key=lambda r: r.importance, reverse=True)

        # Limit
        return results[:query.limit]

    def update(self, memory_id: str, **fields) -> MemoryRecord | None:
        record = self._records.get(memory_id)
        if record is None:
            return None
        for key, val in fields.items():
            if hasattr(record, key):
                setattr(record, key, val)
        return record

    def delete(self, memory_id: str) -> None:
        self._records.pop(memory_id, None)

    def count(self, user_id: str | None = None) -> int:
        if user_id:
            return sum(1 for r in self._records.values()
                       if r.user_id == user_id)
        return len(self._records)

    def clear(self) -> None:
        self._records.clear()

    # ── helpers ──

    @staticmethod
    def _is_expired(record: MemoryRecord) -> bool:
        if not record.expires_at:
            return False
        try:
            return datetime.now(UTC) > datetime.fromisoformat(record.expires_at)
        except (ValueError, TypeError):
            return False
