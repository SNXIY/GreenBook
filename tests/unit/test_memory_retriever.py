from __future__ import annotations

import pytest

from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryRecord,
    MemoryRetriever,
    MemoryType,
)


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
