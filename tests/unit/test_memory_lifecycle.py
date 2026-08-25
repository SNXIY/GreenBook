from __future__ import annotations

import pytest
from greenbook_agent_core.conversation import MemoryUserPreferenceProvider
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryQuery,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
    PreferenceMemoryService,
    PreferenceRetriever,
)
from greenbook_agent_core.memory.policy import MemoryWriteDecision


def _record(
    *,
    key: str,
    value: str,
    source_id: str,
    conversation_id: str,
    confidence: float = 0.7,
) -> MemoryRecord:
    return MemoryRecord(
        user_id="u1",
        tenant_id="tenant-a",
        source_conversation_id=conversation_id,
        memory_type=MemoryType.PREFERENCE,
        content=f"{key}: {value}",
        structured_metadata={
            "preference_type": key,
            "value": value,
        },
        confidence=confidence,
        source_type="CONVERSATION_PREFERENCE_EXTRACTION",
        source_id=source_id,
    )


def test_same_preference_identity_merges_and_updates_confidence() -> None:
    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository)
    first = manager.remember(_record(
        key="writing_depth",
        value="prefer concise articles",
        source_id="source-1",
        conversation_id="conversation-1",
        confidence=0.7,
    ))

    merged = manager.remember(_record(
        key="writing_depth",
        value="prefer concise articles",
        source_id="source-2",
        conversation_id="conversation-2",
        confidence=0.95,
    ))

    assert merged.memory_id == first.memory_id
    assert merged.status == MemoryStatus.ACTIVE
    assert merged.confidence == 0.95
    assert merged.metadata["evidence_count"] == 2
    assert merged.metadata["source_conversation_ids"] == [
        "conversation-1",
        "conversation-2",
    ]
    assert repository.count("u1") == 1


def test_new_value_supersedes_old_value_but_keeps_history() -> None:
    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository)
    old = manager.remember(_record(
        key="technology_stack",
        value="use Python technology stack",
        source_id="source-python",
        conversation_id="conversation-python",
    ))

    new = manager.remember(_record(
        key="technology_stack",
        value="use Java technology stack",
        source_id="source-java",
        conversation_id="conversation-java",
        confidence=0.9,
    ))

    assert old.memory_id != new.memory_id
    assert repository.get(old.memory_id).status == MemoryStatus.SUPERSEDED
    assert repository.get(new.memory_id).status == MemoryStatus.ACTIVE
    active = repository.search(MemoryQuery(
        user_id="u1",
        tenant_id="tenant-a",
        type=MemoryType.PREFERENCE,
        status=MemoryStatus.ACTIVE,
        limit=10,
    ))
    assert [item.memory_id for item in active] == [new.memory_id]
    assert repository.count("u1") == 2


@pytest.mark.asyncio
async def test_status_lifecycle_is_scoped_and_retrieval_excludes_inactive() -> None:
    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository)
    record = manager.remember(_record(
        key="response_style",
        value="prefer concise replies",
        source_id="source-concise",
        conversation_id="conversation-1",
    ))

    assert manager.deactivate(
        record.memory_id,
        user_id="wrong-user",
        tenant_id="tenant-a",
    ) is None
    assert repository.get(record.memory_id).status == MemoryStatus.ACTIVE

    inactive = manager.deactivate(
        record.memory_id,
        user_id="u1",
        tenant_id="tenant-a",
    )
    assert inactive is not None
    assert inactive.status == MemoryStatus.INACTIVE
    assert await PreferenceRetriever(repository).retrieve(
        user_id="u1",
        tenant_id="tenant-a",
        query="concise replies",
    ) == []


def test_supersede_is_tenant_scoped() -> None:
    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository)
    record = manager.remember(_record(
        key="title_style",
        value="avoid clickbait titles",
        source_id="source-title",
        conversation_id="conversation-1",
    ))

    assert manager.supersede(
        record.memory_id,
        user_id="u1",
        tenant_id="other-tenant",
    ) is None
    assert repository.get(record.memory_id).status == MemoryStatus.ACTIVE


def test_disabled_preference_service_does_not_write() -> None:
    repository = InMemoryMemoryRepository()
    service = PreferenceMemoryService(
        MemoryManager(repository),
        enabled=False,
    )

    extraction, record = service.process_completed_turn(
        user_id="u1",
        tenant_id="tenant-a",
        conversation_id="conversation-1",
        user_message="以后写文章标题不要太夸张",
    )

    assert extraction.decision == MemoryWriteDecision.SKIP
    assert extraction.reason == "memory_feature_disabled"
    assert record is None
    assert repository.count("u1") == 0


@pytest.mark.asyncio
async def test_preference_provider_read_does_not_touch_memory() -> None:
    repository = InMemoryMemoryRepository()
    manager = MemoryManager(repository)
    record = manager.remember(_record(
        key="response_style",
        value="prefer concise replies",
        source_id="source-provider",
        conversation_id="conversation-provider",
    ))
    provider = MemoryUserPreferenceProvider(
        manager,
        minimum_observations=1,
    )

    preferences = await provider.list_preferences(
        user_id="u1",
        tenant_id="tenant-a",
    )

    assert preferences
    assert repository.get(record.memory_id).access_count == 0
