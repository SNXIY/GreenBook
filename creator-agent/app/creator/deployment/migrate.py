from __future__ import annotations

import asyncio
import logging
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import Settings, get_settings
from app.creator.runtime.checkpoints import open_creator_checkpointer

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


async def has_unversioned_creator_schema(settings: Settings) -> bool:
    engine = create_async_engine(
        settings.creator_database_url,
        poolclass=NullPool,
    )
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(
                    inspect(sync_connection).get_table_names()
                )
            )
        return (
            "creator_tasks" in tables
            and "creator_alembic_version" not in tables
        )
    finally:
        await engine.dispose()


async def setup_checkpoint_schema(settings: Settings) -> None:
    async with open_creator_checkpointer(settings, ensure_schema=True):
        pass


def migrate(settings: Settings) -> None:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.attributes["creator_database_url"] = settings.creator_database_url
    if asyncio.run(has_unversioned_creator_schema(settings)):
        logger.info(
            "Existing Creator schema detected; recording the current migration head"
        )
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")
    asyncio.run(setup_checkpoint_schema(settings))


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    logger.info("Applying Creator application and checkpoint migrations")
    migrate(settings)
    logger.info("Creator migrations are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
