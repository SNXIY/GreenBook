from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings
from app.creator.application.harness import CreatorAgentHarness, CreatorHarnessPolicy
from app.creator.infrastructure.database import CreatorDatabase
from app.creator.memory.composition import open_creator_memory
from app.creator.model_client import CreatorModelClient
from app.creator.retrieval.composition import open_creator_retrieval
from app.creator.runtime.checkpoints import open_creator_checkpointer
from app.creator.runtime.composition import build_creator_runtime
from app.creator.tools.composition import build_creator_community_provider
from app.creator.worker.service import (
    CreatorOutboxWorker,
    CreatorOutboxWorkerPolicy,
)


@dataclass(frozen=True)
class CreatorWorkerRuntime:
    worker: CreatorOutboxWorker
    database: CreatorDatabase


@asynccontextmanager
async def open_creator_worker_runtime(
    settings: Settings,
) -> AsyncIterator[CreatorWorkerRuntime]:
    database = CreatorDatabase.from_settings(settings)
    try:
        if settings.creator_worker_create_schema:
            await database.create_schema_for_development()
        async with AsyncExitStack() as stack:
            memory = await stack.enter_async_context(
                open_creator_memory(settings=settings, database=database)
            )
            retrieval = await stack.enter_async_context(
                open_creator_retrieval(settings=settings, database=database)
            )
            runtime_community = build_creator_community_provider(settings)
            stack.push_async_callback(runtime_community.aclose)
            checkpointer = await stack.enter_async_context(
                open_creator_checkpointer(settings)
            )
            runtime = build_creator_runtime(
                settings=settings,
                ai_client=CreatorModelClient(settings),
                artifact_store=database.artifact_store,
                checkpointer=checkpointer,
                memory=memory,
                retrieval=retrieval,
                community=runtime_community,
            )
            harness = CreatorAgentHarness(
                uow_factory=database.uow_factory,
                runtime=runtime,
                policy=CreatorHarnessPolicy.from_settings(settings),
                task_memory=memory,
            )
            worker = CreatorOutboxWorker(
                uow_factory=database.uow_factory,
                harness=harness,
                policy=CreatorOutboxWorkerPolicy(
                    worker_prefix=settings.creator_worker_id,
                    concurrency=settings.creator_worker_concurrency,
                    batch_size=settings.creator_worker_batch_size,
                    poll_seconds=settings.creator_worker_poll_seconds,
                    outbox_lease_seconds=(settings.creator_worker_outbox_lease_seconds),
                    heartbeat_seconds=settings.creator_worker_heartbeat_seconds,
                    max_attempts=settings.creator_worker_max_attempts,
                    retry_base_seconds=(settings.creator_worker_retry_base_seconds),
                    retry_max_seconds=settings.creator_worker_retry_max_seconds,
                    shutdown_grace_seconds=(
                        settings.creator_worker_shutdown_grace_seconds
                    ),
                    tenant_concurrency=settings.creator_tenant_max_concurrent_runs,
                    user_concurrency=settings.creator_user_max_concurrent_runs,
                    health_file=(
                        Path(settings.creator_worker_health_file)
                        if settings.creator_worker_health_file.strip()
                        else None
                    ),
                ),
            )
            yield CreatorWorkerRuntime(worker=worker, database=database)
    finally:
        await database.dispose()
