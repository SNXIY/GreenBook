from __future__ import annotations

import asyncio
import logging
import os

from greenbook_assistant_core.execution.persistence_provider import RuntimePersistenceFactory
from greenbook_assistant_core.execution.retry_manager import RetryManager
from greenbook_assistant_core.execution.retry_scheduler import RetryScheduler
from greenbook_assistant_core.execution.retry_worker import RetryBackgroundWorker
from greenbook_assistant_core.execution.runtime_manager import RuntimeManager
from greenbook_assistant_core.execution.state_manager import ExecutionStateManager
from greenbook_java_client import JavaClient

logger = logging.getLogger(__name__)


async def main() -> None:
    """Run Runtime background work from a separately deployable process.

    The retry worker only claims durable retry tasks and hands each task to
    ``RetryManager``. It does not invoke tools directly; execution remains at
    the existing Runtime/Worker boundary.
    """
    java_base = os.getenv("ASSISTANT_JAVA_BASE_URL", "http://127.0.0.1:8080")
    java = JavaClient(java_base)
    persistence = None
    retry_worker = None

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

        logger.info(
            "Assistant worker started java=%s storage=%s worker_id=%s",
            java_base,
            persistence.storage,
            worker_id,
        )
        await retry_worker.run()
    except asyncio.CancelledError:
        logger.info("Worker shutting down")
    finally:
        if retry_worker is not None:
            await retry_worker.shutdown()
        if persistence is not None:
            persistence.close()
        await java.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
