"""Standalone moderation worker process."""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from agents.moderation.graph import moderation_agent
from core import settings
from memory import initialize_database, initialize_store
from moderation.services.runtime import initialize_moderation_services
from moderation.services.worker import ModerationWorkerLoop

load_dotenv()
logger = logging.getLogger(__name__)


async def _run() -> None:
    logging.basicConfig(level=settings.LOG_LEVEL.to_logging_level())
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    async with initialize_database() as saver, initialize_store() as store:
        if hasattr(saver, "setup"):
            await saver.setup()
        if hasattr(store, "setup"):
            await store.setup()

        moderation_agent.checkpointer = saver
        moderation_agent.store = store

        async with initialize_moderation_services(moderation_agent) as services:

            worker = ModerationWorkerLoop(services.workflow)
            services.callback_dispatcher.start()
            try:
                await worker.run_forever()
            except asyncio.CancelledError:
                logger.info("Moderation worker cancelled")
                raise
            finally:
                await services.callback_dispatcher.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Moderation worker interrupted")
