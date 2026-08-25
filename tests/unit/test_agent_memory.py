"""Phase 6.6 tests for Agent Memory Runtime."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from greenbook_agent_core.memory.manager import MemoryManager
from greenbook_agent_core.memory.models import (
    MemoryQuery,
    MemoryRecord,
    MemoryType,
)
from greenbook_agent_core.memory.repository import InMemoryMemoryRepository

# ── Model tests ─────────────────────────────────────────────────

def test_record_defaults() -> None:
    r = MemoryRecord(user_id="u1", type=MemoryType.EPISODIC)
    assert r.memory_id
    assert r.importance == 0.5
    assert r.access_count == 0


def test_query_defaults() -> None:
    q = MemoryQuery()
    assert q.limit == 10
    assert q.sort_by == "importance"


# ── Store: save + find ──────────────────────────────────────────

def test_store_save_and_find() -> None:
    store = InMemoryMemoryRepository()
    r = MemoryRecord(user_id="u1", type=MemoryType.EPISODIC,
                     content="Created Java article")
    store.save(r)
    assert store.find_by_id(r.memory_id) is not None
    assert store.count("u1") == 1


# ── Store: search by type ───────────────────────────────────────

def test_store_search_by_type() -> None:
    store = InMemoryMemoryRepository()
    store.save(MemoryRecord(user_id="u1", type=MemoryType.EPISODIC,
                            content="Task A"))
    store.save(MemoryRecord(user_id="u1", type=MemoryType.SEMANTIC,
                            content="Prefers Java"))
    results = store.search(MemoryQuery(user_id="u1", type=MemoryType.SEMANTIC))
    assert len(results) == 1
    assert results[0].type == MemoryType.SEMANTIC


# ── Store: keyword search ───────────────────────────────────────

def test_store_keyword_search() -> None:
    store = InMemoryMemoryRepository()
    store.save(MemoryRecord(user_id="u1", content="Created Java article"))
    store.save(MemoryRecord(user_id="u1", content="Created Python article"))
    results = store.search(MemoryQuery(user_id="u1", keywords=["Java"]))
    assert len(results) == 1
    assert "Java" in results[0].content


# ── Store: importance sort ──────────────────────────────────────

def test_store_importance_sort() -> None:
    store = InMemoryMemoryRepository()
    store.save(MemoryRecord(user_id="u1", content="Low", importance=0.2))
    store.save(MemoryRecord(user_id="u1", content="High", importance=0.9))
    store.save(MemoryRecord(user_id="u1", content="Mid", importance=0.5))
    results = store.search(MemoryQuery(user_id="u1"))
    assert results[0].importance >= results[1].importance
    assert results[1].importance >= results[2].importance


# ── Store: metadata filter ──────────────────────────────────────

def test_store_metadata_filter() -> None:
    store = InMemoryMemoryRepository()
    store.save(MemoryRecord(user_id="u1", content="A",
                            metadata={"task_id": "t1"}))
    store.save(MemoryRecord(user_id="u1", content="B",
                            metadata={"task_id": "t2"}))
    results = store.search(MemoryQuery(
        user_id="u1", metadata_filters={"task_id": "t1"},
    ))
    assert len(results) == 1
    assert results[0].content == "A"


# ── Store: min_importance filter ────────────────────────────────

def test_store_min_importance() -> None:
    store = InMemoryMemoryRepository()
    store.save(MemoryRecord(user_id="u1", content="Low", importance=0.2))
    store.save(MemoryRecord(user_id="u1", content="High", importance=0.8))
    results = store.search(MemoryQuery(user_id="u1", min_importance=0.5))
    assert len(results) == 1
    assert results[0].content == "High"


# ── Store: update ───────────────────────────────────────────────

def test_store_update() -> None:
    store = InMemoryMemoryRepository()
    r = MemoryRecord(user_id="u1", content="Original")
    store.save(r)
    updated = store.update(r.memory_id, content="Updated", importance=0.9)
    assert updated is not None
    assert updated.content == "Updated"
    assert updated.importance == 0.9


# ── Store: delete ───────────────────────────────────────────────

def test_store_delete() -> None:
    store = InMemoryMemoryRepository()
    r = MemoryRecord(user_id="u1")
    store.save(r)
    assert store.count() == 1
    store.delete(r.memory_id)
    assert store.count() == 0


# ── Store: expire ───────────────────────────────────────────────

def test_store_expire() -> None:
    store = InMemoryMemoryRepository()
    r = MemoryRecord(user_id="u1", content="Ephemeral",
                     expires_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    store.save(r)
    # Expired records should be excluded from search
    results = store.search(MemoryQuery(user_id="u1"))
    assert len(results) == 0


def test_store_not_expired_found() -> None:
    store = InMemoryMemoryRepository()
    r = MemoryRecord(user_id="u1", content="Valid",
                     expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    store.save(r)
    results = store.search(MemoryQuery(user_id="u1"))
    assert len(results) == 1


# ── Manager: recall updates access_count ────────────────────────

def test_manager_recall_updates_access_count() -> None:
    mgr = MemoryManager()
    mgr.remember(MemoryRecord(user_id="u1", content="Test"))
    results = mgr.recall(MemoryQuery(user_id="u1"))
    assert results[0].access_count == 1
    # Recall again
    results2 = mgr.recall(MemoryQuery(user_id="u1"))
    assert results2[0].access_count == 2


# ── Manager: remember_execution ─────────────────────────────────

def test_manager_remember_execution() -> None:
    mgr = MemoryManager()
    r = mgr.remember_execution(
        user_id="u1", goal="Create Java article",
        category="CREATE_CONTENT", status="COMPLETED",
        draft_id="d1",
    )
    assert r.type == MemoryType.EPISODIC
    assert r.metadata["draft_id"] == "d1"
    assert r.importance >= 0.7  # COMPLETED + has draft


# ── Manager: remember_preference ────────────────────────────────

def test_manager_remember_preference() -> None:
    mgr = MemoryManager()
    r = mgr.remember_preference(
        user_id="u1", preference_type="writing_style",
        value="practical_with_code", confidence=0.8,
    )
    assert r.type == MemoryType.SEMANTIC
    assert r.metadata["preference_type"] == "writing_style"
    assert r.metadata["confidence"] == 0.8


# ── Manager: remember_pattern ───────────────────────────────────

def test_manager_remember_pattern() -> None:
    mgr = MemoryManager()
    r = mgr.remember_pattern(
        user_id="u1",
        pattern="CREATE_AND_IMPROVE → higher satisfaction",
        success=True,
    )
    assert r.type == MemoryType.PROCEDURAL
    assert r.importance == 0.3  # starts low


# ── Manager: forget ─────────────────────────────────────────────

def test_manager_forget() -> None:
    mgr = MemoryManager()
    r = mgr.remember(MemoryRecord(user_id="u1", content="To delete"))
    assert mgr.store.count() == 1
    mgr.forget(r.memory_id)
    assert mgr.store.count() == 0


# ── Manager: write dedup by source ──────────────────────────────

def test_manager_remember_dedupes_by_source() -> None:
    """The same logical fact (same user + source) must update the existing
    record in place, never insert a duplicate row (design goal 0813 —
    idempotent memory writes under re-delivery / retry)."""
    mgr = MemoryManager()
    first = mgr.remember(MemoryRecord(
        user_id="u1", source_type="EXECUTION_OUTCOME", source_id="exec-1",
        content="[COMPLETED] goal", importance=0.7,
    ))
    second = mgr.remember(MemoryRecord(
        user_id="u1", source_type="EXECUTION_OUTCOME", source_id="exec-1",
        content="[COMPLETED] goal (refreshed)", importance=0.8,
    ))

    assert mgr.store.count() == 1
    assert second.memory_id == first.memory_id
    stored = mgr.store.get(second.memory_id)
    assert stored is not None
    assert stored.content == "[COMPLETED] goal (refreshed)"
    assert stored.created_at == first.created_at


def test_manager_remember_keeps_distinct_sources() -> None:
    mgr = MemoryManager()
    mgr.remember(MemoryRecord(
        user_id="u1", source_type="EXECUTION_OUTCOME", source_id="exec-1",
        content="A",
    ))
    mgr.remember(MemoryRecord(
        user_id="u1", source_type="EXECUTION_OUTCOME", source_id="exec-2",
        content="B",
    ))
    assert mgr.store.count() == 2


def test_manager_remember_execution_is_idempotent_per_execution() -> None:
    mgr = MemoryManager()
    first = mgr.remember_execution(
        user_id="u1", goal="Create Java article",
        category="CREATE_CONTENT", status="COMPLETED",
        draft_id="d1", execution_id="exec-42",
    )
    second = mgr.remember_execution(
        user_id="u1", goal="Create Java article",
        category="CREATE_CONTENT", status="COMPLETED",
        draft_id="d1", execution_id="exec-42",
    )
    assert mgr.store.count() == 1
    assert second.memory_id == first.memory_id
    assert second.metadata["execution_id"] == "exec-42"


# ── Manager: durable persistence failure handling ───────────────

class _FailingDurableRepository:
    """Sync durable store that is down: save raises immediately."""

    def __init__(self, fail: bool = True) -> None:
        self.fail = fail
        self.saved: list[MemoryRecord] = []

    def save(self, record: MemoryRecord) -> MemoryRecord:
        if self.fail:
            raise RuntimeError("durable store down")
        self.saved.append(record)
        return record

    def delete(self, memory_id: str) -> None:
        return None


def test_manager_sync_durable_failure_is_observed_and_record_kept() -> None:
    """A durable persistence failure must not crash the write path or vanish
    silently: the memory stays in-process and the failure is logged."""
    mgr = MemoryManager(durable_repository=_FailingDurableRepository())
    record = mgr.remember(MemoryRecord(user_id="u1", content="Session fact"))

    assert mgr.store.count() == 1
    assert mgr.recall(MemoryQuery(user_id="u1"))[0].content == "Session fact"
    assert record.memory_id


class _AsyncFailingDurableRepository:
    """Async durable store that is down: save raises when awaited."""

    def __init__(self) -> None:
        self.calls = 0

    async def save(self, record: MemoryRecord) -> MemoryRecord:
        self.calls += 1
        raise RuntimeError("durable store down (async)")

    def delete(self, memory_id: str) -> None:
        return None


@pytest.mark.asyncio
async def test_manager_async_durable_failure_is_guarded() -> None:
    """A fire-and-forget durable save that fails must be guarded and logged,
    never an unobserved task exception."""
    import asyncio

    durable = _AsyncFailingDurableRepository()
    mgr = MemoryManager(durable_repository=durable)
    mgr.remember(MemoryRecord(user_id="u1", content="Async fact"))
    # Let the guarded fire-and-forget task run and observe the failure.
    await asyncio.sleep(0.05)

    assert durable.calls >= 1
    assert mgr.store.count() == 1
