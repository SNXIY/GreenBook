from __future__ import annotations

import asyncio
import logging
import os

from greenbook_assistant_core.execution.execution_queue_worker import (
    ExecutionHandler,
    ExecutionQueueWorker,
)
from greenbook_assistant_core.execution.persistence_provider import RuntimePersistenceFactory
from greenbook_assistant_core.execution.retry_manager import RetryManager
from greenbook_assistant_core.execution.retry_scheduler import RetryScheduler
from greenbook_assistant_core.execution.retry_worker import RetryBackgroundWorker
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_assistant_core.observability.metrics import MemoryMetricsCollector
from greenbook_java_client import JavaClient

logger = logging.getLogger(__name__)


async def main(*, execution_handler: ExecutionHandler | None = None) -> None:
    """Run Runtime background work from a separately deployable process.

    The retry worker only claims durable retry tasks and hands each task to
    ``RetryManager``. An injected ``execution_handler`` enables the same
    process to consume the durable Execution Queue; the queue worker delegates
    execution and never invokes tools directly.
    """
    java_base = os.getenv("ASSISTANT_JAVA_BASE_URL") or os.getenv(
        "GREENBOOK_JAVA_BASE_URL",
        "http://127.0.0.1:8080",
    )
    java_factory = getattr(JavaClient, "from_env", None)
    java = (
        java_factory(base_url=java_base)
        if callable(java_factory)
        else JavaClient(java_base)
    )
    persistence = None
    retry_worker = None
    execution_queue_worker = None
    creator = None
    llm = None

    poll_interval = float(
        os.getenv("ASSISTANT_RETRY_POLL_INTERVAL_SECONDS", "1")
    )
    batch_size = int(os.getenv("ASSISTANT_RETRY_BATCH_SIZE", "20"))
    lease_seconds = int(os.getenv("ASSISTANT_RETRY_LEASE_SECONDS", "60"))
    worker_id = os.getenv(
        "ASSISTANT_RETRY_WORKER_ID",
        "assistant-retry-worker",
    )

    try:
        persistence = RuntimePersistenceFactory.from_env()
        queue_consumer_flag = os.getenv(
            "ASSISTANT_EXECUTION_QUEUE_CONSUMER",
            "true" if persistence.storage == RuntimePersistenceFactory.POSTGRES else "false",
        ).strip().lower()
        metrics_collector = MemoryMetricsCollector()
        state_manager = ExecutionStateManager(
            repository=persistence.execution_repository,
            event_store=persistence.execution_event_store,
        )
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
                from greenbook_creator_client.client import CreatorClient
                from greenbook_mcp_server.server import GreenBookMCPServer
                from openai import AsyncOpenAI

                from .execution_handler import RuntimeExecutionQueueHandler

                creator_base = os.getenv("ASSISTANT_CREATOR_BASE_URL") or os.getenv(
                    "GREENBOOK_CREATOR_BASE_URL",
                    "http://127.0.0.1:8092",
                )
                creator = CreatorClient.from_env(base_url=creator_base)
                mcp = GreenBookMCPServer(java=java, creator=creator)
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
                execution_handler = RuntimeExecutionQueueHandler(
                    repository=persistence.execution_repository,
                    event_store=persistence.execution_event_store,
                    checkpoint_store=persistence.checkpoint_store,
                    external_operation_store=persistence.external_operation_store,
                    mcp=mcp,
                    worker_access_token=os.getenv(
                        "ASSISTANT_WORKER_ACCESS_TOKEN",
                        "",
                    ),
                    llm=llm,
                    model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
                    metrics_collector=metrics_collector,
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
            "Assistant worker started java=%s storage=%s worker_id=%s",
            java_base,
            persistence.storage,
            worker_id,
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
        if retry_worker is not None:
            await retry_worker.shutdown()
        if execution_queue_worker is not None:
            await execution_queue_worker.shutdown()
        if persistence is not None:
            persistence.close()
        if creator is not None:
            await creator.close()
        if llm is not None:
            await llm.close()
        await java.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
