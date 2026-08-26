from __future__ import annotations

import json

import pytest
from greenbook_agent_core.context import ContextBuilder
from greenbook_agent_core.context.projection import project_interpreter_context
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
    PreferenceRetriever,
)
from greenbook_agent_core.turn import ContextAssembler


def _preference(
    *,
    user_id: str,
    tenant_id: str,
    value: str,
    conversation_id: str = "old-conversation",
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryRecord:
    return MemoryRecord(
        user_id=user_id,
        tenant_id=tenant_id,
        source_conversation_id=conversation_id,
        memory_type=MemoryType.PREFERENCE,
        status=status,
        content=value,
        confidence=0.9,
        structured_metadata={
            "preference_type": "writing_style",
            "value": value,
        },
    )


@pytest.mark.asyncio
async def test_preference_retrieval_is_scoped_and_capped() -> None:
    repository = InMemoryMemoryRepository()
    for index in range(7):
        repository.save(_preference(
            user_id="u1",
            tenant_id="tenant-a",
            value=f"prefer concise replies {index}",
        ))
    repository.save(_preference(
        user_id="u2",
        tenant_id="tenant-a",
        value="other-user",
    ))
    repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-b",
        value="other-tenant",
    ))
    inactive = repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="inactive",
        status=MemoryStatus.INACTIVE,
    ))

    values = await PreferenceRetriever(repository).retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="write concise replies",
        limit=99,
    )

    assert len(values) == 5
    assert all(item.user_id == "u1" for item in values)
    assert all(item.tenant_id == "tenant-a" for item in values)
    assert all(item.status == MemoryStatus.ACTIVE for item in values)
    assert inactive.memory_id not in {item.memory_id for item in values}
    assert all(item.access_count == 0 for item in values)


@pytest.mark.asyncio
async def test_preference_retrieval_fails_closed_without_tenant() -> None:
    repository = InMemoryMemoryRepository()
    repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="private preference",
    ))

    assert await PreferenceRetriever(repository).retrieve(
        user_id="u1",
        tenant_id="",
        query="preference",
    ) == []


@pytest.mark.asyncio
async def test_new_conversation_retrieves_preference_before_interpreter() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="prefer technical deep content",
    ))
    assembler = ContextAssembler(
        ContextBuilder(memory_retriever=PreferenceRetriever(repository)),
    )

    assembled = await assembler.assemble(
        conversation_id="new-conversation",
        user_id="u1",
        tenant_id="tenant-a",
        user_input="Write a technical deep article",
    )

    assert assembled.snapshot.memory_ids_used == [record.memory_id]
    assert assembled.snapshot.user_preferences[0]["value"] == record.content
    provider_context = project_interpreter_context(assembled.to_command_context())
    assert provider_context["user_preferences"]
    assert provider_context["user_preferences"][0]["value"] == record.content
    serialized = json.dumps(provider_context, ensure_ascii=False)
    assert record.memory_id not in serialized
    assert "old-conversation" not in serialized


@pytest.mark.asyncio
async def test_disabled_memory_does_not_retrieve_or_touch() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="prefer concise replies",
    ))
    builder = ContextBuilder(
        memory_retriever=PreferenceRetriever(repository),
        memory_enabled=False,
    )

    snapshot = await builder.build(
        conversation_id="new-conversation",
        user_id="u1",
        tenant_id="tenant-a",
        target_query="写一篇 Java 文章",
        memory_recall=True,
    )

    assert snapshot.recalled_memories == []
    assert snapshot.user_preferences == []
    assert repository.get(record.memory_id).access_count == 0


@pytest.mark.asyncio
async def test_relevance_gate_returns_no_memory_for_unrelated_request() -> None:
    repository = InMemoryMemoryRepository()
    repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="prefer technical deep articles",
    ))
    repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="use Java technology stack",
    ))
    repository.save(_preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="prefer concise replies",
    ))

    values = await PreferenceRetriever(repository).retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="Schedule a post tomorrow about Java runtime",
    )

    assert values == []


@pytest.mark.asyncio
async def test_relevance_gate_enforces_confidence_threshold() -> None:
    repository = InMemoryMemoryRepository()
    low_confidence = _preference(
        user_id="u1",
        tenant_id="tenant-a",
        value="prefer concise replies",
    ).model_copy(update={"confidence": 0.4})
    repository.save(low_confidence)

    values = await PreferenceRetriever(repository).retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="Give concise replies",
    )

    assert values == []
