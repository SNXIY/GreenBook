import asyncio
import logging
import sys

import uvicorn
from dotenv import load_dotenv

from core import settings

load_dotenv()


def windows_selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Force a psycopg-compatible loop with both Uvicorn worker modes on Windows."""
    return asyncio.SelectorEventLoop()


if __name__ == "__main__":
    root_logger = logging.getLogger()
    if root_logger.handlers:
        print(
            f"Warning: Root logger already has {len(root_logger.handlers)} handler(s) configured. "
            f"basicConfig() will be ignored. Current level: {logging.getLevelName(root_logger.level)}"
        )

    logging.basicConfig(level=settings.LOG_LEVEL.to_logging_level())
    # Uvicorn 0.51 creates ProactorEventLoop directly for a non-reloading Windows
    # server, so setting only the global policy is no longer sufficient for psycopg.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn_loop = (
        "run_service:windows_selector_loop_factory" if sys.platform == "win32" else "auto"
    )
    uvicorn.run(
        "service:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.is_dev(),
        timeout_graceful_shutdown=settings.GRACEFUL_SHUTDOWN_TIMEOUT,
        loop=uvicorn_loop,
    )
