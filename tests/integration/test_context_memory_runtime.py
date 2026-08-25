from __future__ import annotations

import pytest
from greenbook_agent_core.context import ContextBuilder, SessionContext
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryRecord,
    MemoryRetriever,
    MemoryType,
)


@pytest.mark.asyncio
async def test_context_snapshot_injects_cross_conversation_preference() -> None:
    repository = InMemoryMemoryRepository()
    repository.save(MemoryRecord(
        user_id="u1",
        conversation_id="old-conversation",
        memory_type=MemoryType.PREFERENCE,
        content="Write articles concisely",
        structured_metadata={"preference_type": "writing_style", "value": "concise"},
        importance=0.9,
        confidence=1.0,
        source_type="USER_EXPLICIT_PREFERENCE",
    ))
    builder = ContextBuilder(memory_retriever=MemoryRetriever(repository))

    snapshot = await builder.build(
        conversation_id="new-conversation",
        user_id="u1",
        tenant_id="t1",
        session=SessionContext(
            conversation_id="new-conversation",
            user_id="u1",
            tenant_id="t1",
        ),
        current_command={"objective": "create an article"},
    )

    assert snapshot.recalled_memories
    assert snapshot.memory_ids_used == [snapshot.recalled_memories[0]["memory_id"]]
    assert snapshot.recalled_memories[0]["structured_metadata"]["value"] == "concise"


@pytest.mark.asyncio
async def test_context_does_not_create_memory_for_normal_chat() -> None:
    repository = InMemoryMemoryRepository()
    builder = ContextBuilder(memory_retriever=MemoryRetriever(repository))
    snapshot = await builder.build(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        current_command={"objective": "say hello"},
    )
    assert snapshot.recalled_memories == []
    assert repository.count("u1") == 0


@pytest.mark.asyncio
async def test_production_style_context_can_skip_long_term_recall() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(MemoryRecord(
        user_id="u1",
        memory_type=MemoryType.PREFERENCE,
        content="Write articles concisely",
        structured_metadata={"preference_type": "writing_style", "value": "concise"},
    ))
    builder = ContextBuilder(memory_retriever=MemoryRetriever(repository))

    snapshot = await builder.build(
        conversation_id="c1",
        user_id="u1",
        current_command={"objective": "create an article"},
        memory_recall=False,
    )

    assert snapshot.recalled_memories == []
    assert repository.get(record.memory_id).access_count == 0


@pytest.mark.asyncio
async def test_context_recall_is_explicit_and_does_not_touch_metadata() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(MemoryRecord(
        user_id="u1",
        memory_type=MemoryType.PREFERENCE,
        content="Write articles concisely",
        structured_metadata={"preference_type": "writing_style", "value": "concise"},
    ))
    builder = ContextBuilder(memory_retriever=MemoryRetriever(repository))

    snapshot = await builder.build(
        conversation_id="c1",
        user_id="u1",
        current_command={"objective": "use my writing preference"},
        memory_recall=True,
    )

    assert snapshot.memory_ids_used == [record.memory_id]
    assert repository.get(record.memory_id).access_count == 0
