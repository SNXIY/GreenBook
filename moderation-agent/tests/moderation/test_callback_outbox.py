from datetime import timedelta
from uuid import uuid4

import pytest

from database import DatabaseManager
from database.base import utc_now
from moderation.models import ModerationTask
from moderation.repositories import (
    ModerationCallbackOutboxRepository,
    ModerationTaskRepository,
)
from moderation.schemas import ModerationTaskCreate, ModerationTaskStatus


@pytest.mark.asyncio
async def test_callback_outbox_retries_and_resets_for_new_task_version(
    tmp_path,
) -> None:
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'callback.db'}")
    tasks = ModerationTaskRepository()
    outbox = ModerationCallbackOutboxRepository()
    try:
        async with database.session() as session:
            task = await tasks.create(
                session,
                task_id=uuid4(),
                thread_id="thread-1",
                request=ModerationTaskCreate(
                    content="待审核内容",
                    trace_id="trace-1",
                ),
            )
            task.status = ModerationTaskStatus.COMPLETED
            task.version = 2
            delivery = await outbox.enqueue(
                session,
                task=task,
                max_attempts=2,
            )
            await session.commit()
            delivery_id = delivery.id

        async with database.session() as session:
            claimed = await outbox.claim_next(
                session,
                worker_id="callback-1",
                lease_seconds=30,
            )
            assert claimed is not None
            assert claimed.attempts == 1
            await session.commit()

        async with database.session() as session:
            assert await outbox.mark_failed(
                session,
                delivery_id=delivery_id,
                worker_id="callback-1",
                expected_attempt=1,
                task_version=2,
                error="java unavailable",
                http_status=503,
                retry_base_seconds=0.1,
                retry_max_seconds=1,
            )
            await session.commit()

        async with database.session() as session:
            stored = await session.get(ModerationTask, task.id)
            assert stored is not None
            stored.version = 3
            stored.status = ModerationTaskStatus.COMPLETED
            delivery = await outbox.enqueue(
                session,
                task=stored,
                max_attempts=2,
            )
            await session.commit()

        assert delivery.status == "PENDING"
        assert delivery.task_version == 3
        assert delivery.attempts == 0
        assert delivery.last_error is None
        assert delivery.available_at <= utc_now() + timedelta(seconds=1)
    finally:
        await database.close()
