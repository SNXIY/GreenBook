import asyncio
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from core import settings
from database import DatabaseManager
from database.base import utc_now
from moderation.models import ModerationTask
from moderation.repositories import TaskStateConflictError
from moderation.schemas import ModerationTaskCreate, ModerationTaskStatus
from moderation.services import ModerationWorkflowService


class BlockingGraph:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def ainvoke(
        self,
        input: dict[str, Any] | Command,
        config: RunnableConfig,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del input, config, kwargs
        self.started.set()
        await self.release.wait()
        return {}


@pytest.mark.asyncio
async def test_expired_worker_cannot_overwrite_reclaimed_task(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "MODERATION_ASYNC_ENABLED", True)
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'worker-fencing.db'}")
    graph = BlockingGraph()
    service = ModerationWorkflowService(database=database, graph=graph)
    try:
        accepted = await service.create_task(
            ModerationTaskCreate(content="需要审核的帖子")
        )
        task_id = accepted.task.id
        assert await service.claim_next_task(worker_id="worker-old") == task_id

        execution = asyncio.create_task(service.process_task(task_id))
        await graph.started.wait()

        async with database.session() as session:
            task = await session.get(ModerationTask, task_id)
            assert task is not None
            task.status = ModerationTaskStatus.RUNNING
            task.locked_by = "worker-new"
            task.locked_at = utc_now()
            task.attempt_count += 1
            task.version += 1
            await session.commit()

        graph.release.set()
        with pytest.raises(TaskStateConflictError, match="Stale moderation worker"):
            await execution

        async with database.session() as session:
            stored = await session.get(ModerationTask, task_id)
            assert stored is not None
            assert stored.status == ModerationTaskStatus.RUNNING
            assert stored.locked_by == "worker-new"
            assert stored.attempt_count == 2
            assert stored.agent_decision is None
            assert stored.error_message is None
    finally:
        await database.close()
