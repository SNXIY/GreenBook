from __future__ import annotations

import asyncio

import pytest
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryRecord,
    MemoryRetriever,
    MemoryType,
)
from greenbook_agent_core.observability.run_metrics import snapshot


@pytest.mark.asyncio
async def test_retriever_reranks_java_memory_over_python_memory() -> None:
    repository = InMemoryMemoryRepository()
    java = repository.save(MemoryRecord(
        user_id="u1",
        memory_type=MemoryType.EPISODIC,
        content="The user created a Java tutorial draft successfully.",
        structured_metadata={"artifact_id": "draft-java"},
        importance=0.7,
    ))
    repository.save(MemoryRecord(
        user_id="u1",
        memory_type=MemoryType.EPISODIC,
        content="The user created a Python tutorial draft successfully.",
        structured_metadata={"artifact_id": "draft-python"},
        importance=0.99,
    ))

    values = await MemoryRetriever(repository).retrieve(
        user_id="u1",
        command={"objective": "modify the Java article"},
        limit=1,
    )

    assert len(values) == 1
    assert values[0].memory_id == java.memory_id
    assert values[0].access_count == 1


@pytest.mark.asyncio
async def test_retriever_isolated_by_user_and_empty_results_are_safe() -> None:
    repository = InMemoryMemoryRepository()
    repository.save(MemoryRecord(user_id="u1", content="concise style"))
    repository.save(MemoryRecord(user_id="u2", content="verbose style"))
    values = await MemoryRetriever(repository).retrieve(
        user_id="u1",
        command={"objective": "unrelated astronomy"},
    )
    assert all(item.user_id == "u1" for item in values)


@pytest.mark.asyncio
async def test_retriever_records_actual_search_ranking_and_touch_stages() -> None:
    repository = InMemoryMemoryRepository()
    repository.save(MemoryRecord(user_id="u-metrics", content="Java draft facts"))

    await MemoryRetriever(repository).retrieve(
        user_id="u-metrics",
        command={"objective": "Java draft"},
        run_id="run-memory-metrics",
    )

    metrics = snapshot("run-memory-metrics")
    durations = metrics["stage_durations_ms"]
    assert durations["memory_retrieval_ms"] is not None
    assert durations["memory_repository_search_ms"] is not None
    assert durations["memory_ranking_filter_ms"] is not None
    assert durations["memory_touch_ms"] is not None
    assert metrics["memory_retrieval"]["source"] == "repository"
    assert metrics["memory_retrieval"]["candidate_count"] == 1


@pytest.mark.asyncio
async def test_retriever_can_skip_touch_for_context_reads() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(MemoryRecord(user_id="u-no-touch", content="Java preference"))

    values = await MemoryRetriever(repository).retrieve(
        user_id="u-no-touch",
        command={"objective": "Java"},
        touch=False,
    )

    assert values[0].memory_id == record.memory_id
    assert repository.get(record.memory_id).access_count == 0


@pytest.mark.asyncio
async def test_repository_type_queries_are_concurrent_but_results_are_deduped() -> None:
    records = [
        MemoryRecord(
            user_id="u-io",
            tenant_id="tenant-io",
            memory_type=MemoryType.EPISODIC,
            content="Java draft fact",
        ),
        MemoryRecord(
            user_id="u-io",
            tenant_id="tenant-io",
            memory_type=MemoryType.PROCEDURAL,
            content="Java draft procedure",
        ),
    ]
    active = 0
    maximum = 0

    class Repository:
        async def search(self, query):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return [item for item in records if item.memory_type == query.type]

    values = await MemoryRetriever(
        Repository(),
        memory_types=[MemoryType.EPISODIC, MemoryType.PROCEDURAL],
        require_tenant_scope=True,
    ).retrieve(
        user_id="u-io",
        tenant_id="tenant-io",
        target_query="Java draft",
        touch=False,
    )

    assert maximum == 2
    assert values
    assert len({item.memory_id for item in values}) == len(values)
