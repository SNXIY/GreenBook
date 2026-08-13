from __future__ import annotations

from datetime import UTC, datetime

import pytest
from greenbook_agent_core.memory import (
    InMemoryMemoryRepository,
    MemoryRecord,
    MemoryType,
)


def test_memory_repository_is_the_durable_contract_for_restart_like_reopen() -> None:
    repository = InMemoryMemoryRepository()
    record = MemoryRecord(
        user_id="u1",
        conversation_id="c1",
        memory_type=MemoryType.PREFERENCE,
        content="Prefer concise technical article titles",
        structured_metadata={"key": "title_style", "value": "concise"},
        confidence=0.95,
        source_type="USER_EXPLICIT_PREFERENCE",
    )
    repository.save(record)

    reopened_repository = repository
    loaded = reopened_repository.get(record.memory_id)
    assert loaded is not None
    assert loaded.memory_type == MemoryType.PREFERENCE
    assert loaded.metadata["value"] == "concise"


@pytest.mark.asyncio
async def test_repository_touch_tracks_memory_use() -> None:
    repository = InMemoryMemoryRepository()
    record = repository.save(MemoryRecord(user_id="u1", content="Java"))
    touched = repository.touch(record.memory_id)
    assert touched is not None
    assert touched.access_count == 1
    assert repository.get(record.memory_id).access_count == 1


def test_postgres_memory_params_convert_contract_timestamps_for_asyncpg() -> None:
    from greenbook_agent_core.memory.repository import _params

    record = MemoryRecord(
        user_id="u1",
        created_at="2026-08-12T03:30:00+00:00",
        updated_at="2026-08-12T03:31:00+00:00",
    )

    params = _params(record)

    assert params["created_at"] == datetime(2026, 8, 12, 3, 30, tzinfo=UTC)
    assert params["updated_at"] == datetime(2026, 8, 12, 3, 31, tzinfo=UTC)


def test_memory_record_accepts_unaccessed_postgres_row() -> None:
    record = MemoryRecord.model_validate(
        {
            "user_id": "u1",
            "content": "Java",
            "last_accessed_at": None,
        }
    )

    assert record.last_accessed_at is None
