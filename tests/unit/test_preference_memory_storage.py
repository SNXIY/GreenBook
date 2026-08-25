from __future__ import annotations

from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryManager,
    MemoryQuery,
    MemoryRecord,
    MemoryStatus,
    MemoryType,
)


def _preference(**overrides: object) -> MemoryRecord:
    values: dict[str, object] = {
        "user_id": "user-1",
        "tenant_id": "tenant-a",
        "memory_type": MemoryType.PREFERENCE,
        "content": "Avoid exaggerated article titles",
        "confidence": 0.9,
        "source_conversation_id": "conversation-old",
        "status": MemoryStatus.ACTIVE,
        "source_type": "USER_EXPLICIT_PREFERENCE",
        "source_id": "preference-title-style",
    }
    values.update(overrides)
    return MemoryRecord.model_validate(values)


def test_preference_record_exposes_required_scoped_fields() -> None:
    record = _preference()

    assert record.memory_id
    assert record.user_id == "user-1"
    assert record.tenant_id == "tenant-a"
    assert record.memory_type == MemoryType.PREFERENCE
    assert record.confidence == 0.9
    assert record.source_conversation_id == "conversation-old"
    assert record.conversation_id == "conversation-old"
    assert record.status == MemoryStatus.ACTIVE
    assert record.created_at
    assert record.updated_at


def test_preference_storage_crud_is_scope_checked() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(_preference())

    assert repository.get(
        record.memory_id,
        user_id="user-1",
        tenant_id="tenant-a",
    ) is not None
    assert repository.get(
        record.memory_id,
        user_id="user-2",
        tenant_id="tenant-a",
    ) is None
    assert repository.get(
        record.memory_id,
        user_id="user-1",
        tenant_id="tenant-b",
    ) is None

    updated = repository.update(
        record.memory_id,
        content="Prefer deep technical articles",
        confidence=0.95,
    )
    assert updated is not None
    assert updated.content == "Prefer deep technical articles"
    assert updated.confidence == 0.95

    repository.delete(
        record.memory_id,
        user_id="user-1",
        tenant_id="tenant-b",
    )
    assert repository.get(record.memory_id) is not None
    repository.delete(
        record.memory_id,
        user_id="user-1",
        tenant_id="tenant-a",
    )
    assert repository.get(record.memory_id) is None


def test_preference_search_isolated_by_user_and_tenant() -> None:
    repository = InMemoryMemoryRepository()
    primary = repository.save(_preference())
    repository.save(_preference(
        memory_id="other-user",
        user_id="user-2",
        content="Prefer verbose explanations",
    ))
    repository.save(_preference(
        memory_id="other-tenant",
        tenant_id="tenant-b",
        content="Prefer Python examples",
    ))

    values = repository.search(MemoryQuery(
        user_id="user-1",
        tenant_id="tenant-a",
        type=MemoryType.PREFERENCE,
        status=MemoryStatus.ACTIVE,
    ))

    assert [item.memory_id for item in values] == [primary.memory_id]
    assert all(item.user_id == "user-1" for item in values)
    assert all(item.tenant_id == "tenant-a" for item in values)


def test_memory_manager_preference_write_keeps_tenant_and_provenance() -> None:
    manager = MemoryManager(repository=InMemoryMemoryRepository())

    record = manager.remember_preference(
        "user-1",
        "title_style",
        "avoid clickbait",
        confidence=0.88,
        tenant_id="tenant-a",
        source_conversation_id="conversation-1",
    )

    assert record.tenant_id == "tenant-a"
    assert record.source_conversation_id == "conversation-1"
    assert record.status == MemoryStatus.ACTIVE
    assert record.memory_type == MemoryType.PREFERENCE
    assert manager.store.search(MemoryQuery(
        user_id="user-1",
        tenant_id="tenant-a",
        type=MemoryType.PREFERENCE,
    ))


def test_postgres_params_include_preference_scope_contract() -> None:
    from greenbook_agent_core.memory.repository import _params

    values = _params(_preference())

    assert values["tenant_id"] == "tenant-a"
    assert values["conversation_id"] == "conversation-old"
    assert values["source_conversation_id"] == "conversation-old"
    assert values["status"] == "active"
