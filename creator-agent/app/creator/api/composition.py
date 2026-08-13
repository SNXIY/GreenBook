from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from app.core.config import Settings
from app.creator.api.dispatcher import (
    CreatorLocalRunDispatcher,
    CreatorOutboxRunDispatcher,
    CreatorRunDispatcher,
)
from app.creator.api.identity import validate_creator_identity_settings
from app.creator.api.query import SqlAlchemyCreatorWorkspaceQuery
from app.creator.api.service import CreatorWorkspaceService
from app.creator.application.harness import CreatorAgentHarness, CreatorHarnessPolicy
from app.creator.application.ports import (
    CreatorRuntimePort,
    CreatorTaskMemoryPort,
)
from app.creator.domain.models import (
    RuntimeOutcome,
    RuntimeResumeRequest,
    RuntimeStartRequest,
)
from app.creator.drafts.service import CreatorDraftService
from app.creator.infrastructure.database import CreatorDatabase
from app.creator.memory.composition import open_creator_memory
from app.creator.model_client import CreatorModelClient
from app.creator.publication.service import CreatorPublicationHandoffService
from app.creator.retrieval.composition import open_creator_retrieval
from app.creator.runtime.checkpoints import open_creator_checkpointer
from app.creator.runtime.composition import build_creator_model_gateway, build_creator_runtime
from app.creator.studio.service import CreatorStudioService
from app.creator.tools.composition import build_creator_community_provider

logger = logging.getLogger("uvicorn.error")


@dataclass(frozen=True)
class CreatorApiRuntime:
    workspace: CreatorWorkspaceService
    query: SqlAlchemyCreatorWorkspaceQuery
    dispatcher: CreatorRunDispatcher
    database: CreatorDatabase
    execution_mode: str
    sse_poll_seconds: float
    sse_heartbeat_seconds: float
    sse_send_timeout_seconds: float
    studio: CreatorStudioService | None = None
    model_provider: str = "deepseek"
    model_name: str = ""


class _CommandOnlyCreatorRuntime:
    name = "outbox-api-command-only"

    async def start(self, request: RuntimeStartRequest, **kwargs) -> RuntimeOutcome:
        raise RuntimeError("Creator runtime execution belongs to the external worker")

    async def resume(self, request: RuntimeResumeRequest, **kwargs) -> RuntimeOutcome:
        raise RuntimeError("Creator runtime execution belongs to the external worker")


@asynccontextmanager
async def open_creator_api_runtime(
    settings: Settings,
) -> AsyncIterator[CreatorApiRuntime]:
    validate_creator_identity_settings(settings)
    database = CreatorDatabase.from_settings(settings)
    try:
        if settings.creator_api_create_schema:
            await database.create_schema_for_development()
        async with AsyncExitStack() as stack:
            mode = settings.creator_api_execution_mode.strip().lower()
            if mode not in {"local", "outbox"}:
                raise ValueError(
                    "CREATOR_API_EXECUTION_MODE must be 'local' or 'outbox'"
                )
            runtime: CreatorRuntimePort
            task_memory: CreatorTaskMemoryPort | None
            ai_client = CreatorModelClient(settings)
            model_gateway = build_creator_model_gateway(
                settings=settings,
                ai_client=ai_client,
            )
            if mode == "local":
                memory = await stack.enter_async_context(
                    open_creator_memory(settings=settings, database=database)
                )
                retrieval = await stack.enter_async_context(
                    open_creator_retrieval(settings=settings, database=database)
                )
                community = build_creator_community_provider(settings)
                stack.push_async_callback(community.aclose)
                checkpointer = await stack.enter_async_context(
                    open_creator_checkpointer(settings)
                )
                runtime = build_creator_runtime(
                    settings=settings,
                    ai_client=ai_client,
                    artifact_store=database.artifact_store,
                    checkpointer=checkpointer,
                    memory=memory,
                    retrieval=retrieval,
                    community=community,
                    model_gateway=model_gateway,
                )
                task_memory = memory
            else:
                runtime = _CommandOnlyCreatorRuntime()
                task_memory = None
            harness = CreatorAgentHarness(
                uow_factory=database.uow_factory,
                runtime=runtime,
                policy=CreatorHarnessPolicy.from_settings(settings),
                task_memory=task_memory,
            )
            if mode == "local":
                dispatcher: CreatorRunDispatcher = CreatorLocalRunDispatcher(
                    harness,
                    worker_prefix=settings.creator_api_worker_id,
                    concurrency=settings.creator_api_worker_concurrency,
                    tenant_concurrency=settings.creator_tenant_max_concurrent_runs,
                    user_concurrency=settings.creator_user_max_concurrent_runs,
                    retry_delay_seconds=settings.creator_retry_delay_seconds,
                    shutdown_grace_seconds=(
                        settings.creator_api_shutdown_grace_seconds
                    ),
                )
            else:
                dispatcher = CreatorOutboxRunDispatcher()
            logger.info(
                "Creator API/dispatcher store identity api_store=%s dispatcher_store=%s database=%s pool=%s queue_namespace=%s",
                database.diagnostics()["store_instance_id"],
                database.diagnostics()["store_instance_id"],
                database.diagnostics()["database_absolute_path"] or database.diagnostics()["database_url"],
                database.diagnostics()["connection_pool_identity"],
                settings.creator_queue_namespace,
            )
            query = SqlAlchemyCreatorWorkspaceQuery(
                database.sessions,
                default_page_size=settings.creator_api_default_page_size,
            )
            drafts = CreatorDraftService(database.draft_store)
            publication = CreatorPublicationHandoffService(
                handoffs=database.publication_store,
                drafts=drafts,
                draft_loader=database.draft_store.get,
                java_base_url=settings.creator_publication_java_base_url,
                java_shared_secret=settings.creator_publication_shared_secret,
                java_timeout_seconds=settings.creator_publication_timeout_seconds,
            )
            studio = CreatorStudioService(
                sessions=database.sessions,
                drafts=drafts,
                draft_loader=database.draft_store.get,
                model=model_gateway,
                model_provider=settings.ai_provider.strip().lower(),
                model_name=_creator_model_name(settings),
            )
            workspace = CreatorWorkspaceService(
                harness=harness,
                query=query,
                drafts=drafts,
                publication=publication,
                artifact_store=database.artifact_store,
                sessions=database.sessions,
                dispatcher=dispatcher,
                studio=studio,
                worker_prefix=settings.creator_api_worker_id,
            )
            api_runtime = CreatorApiRuntime(
                workspace=workspace,
                studio=studio,
                query=query,
                dispatcher=dispatcher,
                database=database,
                execution_mode=dispatcher.execution_mode,
                sse_poll_seconds=max(
                    0.1,
                    settings.creator_api_sse_poll_seconds,
                ),
                sse_heartbeat_seconds=max(
                    1.0,
                    settings.creator_api_sse_heartbeat_seconds,
                ),
                sse_send_timeout_seconds=max(
                    1.0,
                    settings.creator_api_sse_send_timeout_seconds,
                ),
                model_provider=settings.ai_provider.strip().lower(),
                model_name=_creator_model_name(settings),
            )
            if mode == "local":
                await dispatcher.recover(await query.list_runnable_run_ids())
            try:
                yield api_runtime
            finally:
                await dispatcher.aclose()
    finally:
        await database.dispose()


def _creator_model_name(settings: Settings) -> str:
    provider = settings.ai_provider.strip().lower()
    if provider == "deepseek":
        return settings.deepseek_model
    if provider == "openai":
        return settings.openai_model
    if provider == "ollama":
        return settings.ollama_model
    raise ValueError(f"Unsupported real model provider: {provider}")
