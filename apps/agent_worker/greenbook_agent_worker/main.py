from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from greenbook_agent_core.execution.execution_queue_worker import (
    ExecutionHandler,
    ExecutionQueueWorker,
)
from greenbook_agent_core.execution.persistence_provider import RuntimePersistenceFactory
from greenbook_agent_core.execution.retry_manager import RetryManager
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.execution.retry_worker import RetryBackgroundWorker
from greenbook_agent_core.execution.runtime_manager import RuntimeManager
from greenbook_agent_core.observability.metrics import MemoryMetricsCollector
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_java_client import JavaClient

logger = logging.getLogger(__name__)

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def _load_project_env() -> None:
    """Load the repository Runtime profile for direct module launches.

    The API loads the root ``.env`` during import, but the standalone worker
    is also launched directly with ``python -m``.  Without this bridge the
    worker silently falls back to the in-memory persistence profile and never
    constructs the PostgreSQL Execution Queue consumer.
    """

    if not _ENV_FILE.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        logger.warning("python-dotenv is unavailable; worker will use process environment only")
        return
    load_dotenv(_ENV_FILE, override=False)


def _worker_health_path() -> Path | None:
    value = os.getenv(
        "GREENBOOK_AGENT_WORKER_HEALTH_FILE",
        ".runtime/agent-worker-health.json",
    ).strip()
    return Path(value) if value else None


def _write_worker_health(
    path: Path | None,
    *,
    status: str,
    storage: str = "",
    queue_consumer: bool = False,
    worker_id: str = "",
    error: str = "",
) -> None:
    if path is None:
        return
    payload = {
        "status": status,
        "updated_at": datetime.now(UTC).isoformat(),
        "pid": os.getpid(),
        "storage": storage,
        "queue_consumer": queue_consumer,
        "worker_id": worker_id,
    }
    if error:
        payload["error"] = error[:500]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        logger.warning("Unable to write worker health file path=%s", path, exc_info=True)


async def _worker_health_heartbeat(
    path: Path,
    *,
    storage: str,
    queue_consumer: bool,
    worker_id: str,
    interval_seconds: float,
) -> None:
    while True:
        _write_worker_health(
            path,
            status="READY",
            storage=storage,
            queue_consumer=queue_consumer,
            worker_id=worker_id,
        )
        await asyncio.sleep(interval_seconds)


async def main(*, execution_handler: ExecutionHandler | None = None) -> None:
    """Run Runtime background work from a separately deployable process.

    The retry worker only claims durable retry tasks and hands each task to
    ``RetryManager``. An injected ``execution_handler`` enables the same
    process to consume the durable Execution Queue; the queue worker delegates
    execution and never invokes tools directly.
    """
    _load_project_env()
    java_base = os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080")
    java_factory = getattr(JavaClient, "from_env", None)
    java = (
        java_factory(base_url=java_base)
        if callable(java_factory)
        else JavaClient(java_base)
    )
    persistence = None
    runtime_container = None
    retry_worker = None
    execution_queue_worker = None
    creator = None
    llm = None
    poll_interval = float(
        os.getenv("GREENBOOK_AGENT_RETRY_POLL_INTERVAL_SECONDS", "1")
    )
    batch_size = int(os.getenv("GREENBOOK_AGENT_RETRY_BATCH_SIZE", "20"))
    lease_seconds = int(os.getenv("GREENBOOK_AGENT_RETRY_LEASE_SECONDS", "60"))
    worker_id = os.getenv(
        "GREENBOOK_AGENT_RETRY_WORKER_ID",
        "agent-retry-worker",
    )
    health_path = _worker_health_path()
    health_task = None
    _write_worker_health(health_path, status="STARTING", worker_id=worker_id)

    try:
        from greenbook_mcp_server import tool_registry as mcp_tool_registry
        from greenbook_security.policy import SecurityPolicy

        runtime_container = RuntimeContainer.from_env(
            tool_registry=mcp_tool_registry,
            security_policy=SecurityPolicy(),
        )
        persistence = runtime_container.persistence
        queue_consumer_flag = os.getenv(
            "GREENBOOK_AGENT_EXECUTION_QUEUE_CONSUMER",
            "true" if persistence.storage == RuntimePersistenceFactory.POSTGRES else "false",
        ).strip().lower()
        metrics_collector = MemoryMetricsCollector()
        state_manager = runtime_container.execution_state_manager
        runtime_manager = RuntimeManager(
            state_manager=state_manager,
            checkpoint_store=persistence.checkpoint_store,
        )
        retry_manager = RetryManager(
            state_manager=state_manager,
            runtime_manager=runtime_manager,
            metrics_collector=metrics_collector,
        )
        scheduler = RetryScheduler(
            task_store=persistence.retry_task_store,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )
        retry_worker = RetryBackgroundWorker(
            scheduler=scheduler,
            retry_manager=retry_manager,
            poll_interval_seconds=poll_interval,
            batch_size=batch_size,
            worker_id=worker_id,
            execution_queue=persistence.execution_queue,
        )
        if execution_handler is not None:
            execution_queue_worker = ExecutionQueueWorker(
                queue=persistence.execution_queue,
                execution_handler=execution_handler,
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                poll_interval_seconds=poll_interval,
                batch_size=batch_size,
                lease_manager=persistence.lease_manager,
            )
        else:
            if queue_consumer_flag in {"1", "true", "yes", "on"}:
                from greenbook_agent_api.services.approval_runtime_service import (
                    ApprovalRuntimeService,
                )
                from greenbook_agent_api.services.execution_completion_publisher import (
                    ExecutionCompletionPublisher,
                )
                from greenbook_agent_api.services.task_provider import TaskProvider
                from greenbook_agent_core.conversation import ConversationService
                from greenbook_agent_core.db.connection import session_ctx
                from greenbook_agent_core.human import PostgresApprovalRequestStore
                from greenbook_agent_core.memory import PostgresMemoryRepository
                from greenbook_agent_core.memory.manager import MemoryManager
                from greenbook_creator_client.client import CreatorClient
                from greenbook_mcp_server.server import GreenBookMCPServer
                from openai import AsyncOpenAI

                from .execution_handler import RuntimeExecutionQueueHandler

                creator_base = os.getenv(
                    "GREENBOOK_CREATOR_BASE_URL",
                    "http://127.0.0.1:8092",
                )
                creator = CreatorClient.from_env(base_url=creator_base)
                mcp = GreenBookMCPServer(
                    java=java,
                    creator=creator,
                    capability_registry=runtime_container.capability_registry,
                )
                llm_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv(
                    "OPENAI_API_KEY",
                    "",
                )
                if llm_key:
                    llm = AsyncOpenAI(
                        api_key=llm_key,
                        base_url=os.getenv(
                            "DEEPSEEK_BASE_URL",
                            "https://api.deepseek.com",
                        ),
                    )
                conversation_service = ConversationService()
                await conversation_service.ensure_storage()
                durable_memory_repository = None
                if persistence.storage == RuntimePersistenceFactory.POSTGRES:
                    durable_memory_repository = PostgresMemoryRepository(session_ctx)
                    await durable_memory_repository.ensure_storage()
                memory_manager = MemoryManager(
                    durable_repository=durable_memory_repository,
                )
                task_provider = TaskProvider()
                await task_provider.ensure_storage()
                approval_service = ApprovalRuntimeService(
                    store=PostgresApprovalRequestStore(),
                    runtime_manager=runtime_manager,
                    state_manager=state_manager,
                    execution_queue=persistence.execution_queue,
                    conversation_service=conversation_service,
                )
                completion_publisher = ExecutionCompletionPublisher(
                    conversation_service=conversation_service,
                    run_store={},
                    artifact_store=persistence.artifact_store,
                    result_projection_store=persistence.result_projection_store,
                    task_provider=task_provider,
                    approval_service=approval_service,
                )
                for queued_message in persistence.execution_queue.list()[-100:]:
                    persisted_execution = persistence.execution_repository.find_by_id(
                        queued_message.execution_id
                    )
                    if persisted_execution is None:
                        continue
                    try:
                        await completion_publisher.reconcile(
                            queued_message,
                            persisted_execution,
                        )
                    except Exception:
                        logger.warning(
                            "Worker result projection reconciliation failed execution_id=%s",
                            queued_message.execution_id,
                            exc_info=True,
                        )
                execution_handler = RuntimeExecutionQueueHandler(
                    repository=persistence.execution_repository,
                    event_store=persistence.execution_event_store,
                    checkpoint_store=persistence.checkpoint_store,
                    external_operation_store=persistence.external_operation_store,
                    mcp=mcp,
                    worker_access_token=os.getenv(
                        "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN",
                        "",
                    ),
                    llm=llm,
                    model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
                    metrics_collector=metrics_collector,
                    retry_scheduler=scheduler,
                    container=runtime_container,
                    completion_publisher=completion_publisher,
                    memory_manager=memory_manager,
                )
                execution_queue_worker = ExecutionQueueWorker(
                    queue=persistence.execution_queue,
                    execution_handler=execution_handler,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    poll_interval_seconds=poll_interval,
                    batch_size=batch_size,
                    lease_manager=persistence.lease_manager,
                )
            else:
                logger.warning(
                    "Execution queue consumer is not enabled: no execution_handler was injected"
                )

        logger.info(
            "Agent worker started java=%s storage=%s worker_id=%s",
            java_base,
            persistence.storage,
            worker_id,
        )
        logger.info(
            "Execution Queue consumer %s worker_id=%s poll_interval_seconds=%s batch_size=%s",
            "enabled" if execution_queue_worker is not None else "disabled",
            worker_id,
            poll_interval,
            batch_size,
        )
        if execution_queue_worker is not None:
            logger.info(
                "Execution consumer started worker_id=%s storage=%s",
                worker_id,
                persistence.storage,
            )
        if health_path is not None:
            heartbeat_interval = max(
                1.0,
                float(os.getenv("GREENBOOK_AGENT_WORKER_HEALTH_INTERVAL_SECONDS", "15")),
            )
            health_task = asyncio.create_task(
                _worker_health_heartbeat(
                    health_path,
                    storage=persistence.storage,
                    queue_consumer=execution_queue_worker is not None,
                    worker_id=worker_id,
                    interval_seconds=heartbeat_interval,
                ),
                name="agent-worker-health-heartbeat",
            )
        if execution_queue_worker is None:
            await retry_worker.run()
        else:
            consumer_tasks = [
                asyncio.create_task(retry_worker.run(), name="retry-consumer"),
                asyncio.create_task(
                    execution_queue_worker.run(),
                    name="execution-consumer",
                ),
            ]
            try:
                await asyncio.gather(*consumer_tasks)
            finally:
                for task in consumer_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*consumer_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("Worker shutting down")
    finally:
        if health_task is not None:
            health_task.cancel()
            await asyncio.gather(health_task, return_exceptions=True)
        if retry_worker is not None:
            await retry_worker.shutdown()
        if execution_queue_worker is not None:
            await execution_queue_worker.shutdown()
        if runtime_container is not None:
            runtime_container.close()
        if creator is not None:
            await creator.close()
        if llm is not None:
            await llm.close()
        await java.close()
        _write_worker_health(
            health_path,
            status="STOPPED",
            storage=persistence.storage if persistence is not None else "",
            queue_consumer=execution_queue_worker is not None,
            worker_id=worker_id,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
