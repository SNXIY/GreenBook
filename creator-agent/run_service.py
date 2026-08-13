"""Local Creator API entry point with a psycopg-compatible Windows event loop."""

import asyncio
import os
import sys

import uvicorn
from dotenv import load_dotenv

load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    uvicorn.run(
        "app.main:app",
        host=os.getenv("GREENBOOK_CREATOR_API_HOST", "127.0.0.1"),
        port=int(os.getenv("GREENBOOK_CREATOR_API_PORT", "8092")),
        reload=_env_flag("GREENBOOK_CREATOR_DEV_RELOAD", default=True),
        loop="asyncio" if sys.platform == "win32" else "auto",
    )
