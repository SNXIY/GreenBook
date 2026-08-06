from __future__ import annotations

import asyncio
import logging
import os

from greenbook_java_client import JavaClient

logger = logging.getLogger(__name__)


async def main() -> None:
    """Minimal async worker — placeholder for Kafka consumers and scheduled jobs.

    In production this consumes:
    - Publication events from Kafka
    - Creator task completion events
    - Analytics aggregation jobs
    """
    java_base = os.getenv("ASSISTANT_JAVA_BASE_URL", "http://127.0.0.1:8080")
    java = JavaClient(java_base)

    logger.info("Assistant worker started java=%s", java_base)

    try:
        # Placeholder: keep alive, ready for Kafka consumers
        while True:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("Worker shutting down")
    finally:
        await java.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
