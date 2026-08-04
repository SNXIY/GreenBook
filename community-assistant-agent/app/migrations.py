from __future__ import annotations

import asyncio
import logging
import os
import sys
from enum import Enum
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


logger = logging.getLogger(__name__)

ASSISTANT_REVISIONS = {
    "001_assistant_baseline",
    "002_harness_controls",
    "003_saga_capabilities",
    "004_capability_revocation",
    "005_runtime_freshness",
    "006_concurrency_latency",
    "007_orchestration_platform",
    "008_four_layer_memory",
    "009_governed_runtime",
    "010_adaptive_execution",
    "011_goal_target_binding",
    "012_intent_deltas",
    "013_target_resolution",
    "014_target_context",
    "015_target_binding_roles",
    "016_intent_delta_target_role",
    "017_artifact_lifecycle",
    "018_intent_delta_operation_class",
    "019_goal_resolution_metadata",
    "020_execution_reliability",
}


class MigrationBootstrap(str, Enum):
    UPGRADE = "upgrade"
    STAMP = "stamp"


def _prepare_version_table(sync_connection) -> MigrationBootstrap:
    tables = set(inspect(sync_connection).get_table_names())
    if "assistant_alembic_version" in tables:
        return MigrationBootstrap.UPGRADE

    if "alembic_version" in tables:
        revision = sync_connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if revision in ASSISTANT_REVISIONS:
            sync_connection.execute(
                text(
                    "ALTER TABLE alembic_version "
                    "RENAME TO assistant_alembic_version"
                )
            )
            logger.info("Preserved the legacy Assistant migration ledger")
            return MigrationBootstrap.UPGRADE

    if "assistant_runs" in tables:
        return MigrationBootstrap.STAMP
    return MigrationBootstrap.UPGRADE


async def prepare_version_table(database_url: str) -> MigrationBootstrap:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            return await connection.run_sync(_prepare_version_table)
    finally:
        await engine.dispose()


def migrate(database_url: str) -> None:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    os.environ["ASSISTANT_DATABASE_URL"] = database_url
    action = asyncio.run(prepare_version_table(database_url))
    if action is MigrationBootstrap.STAMP:
        logger.info(
            "Existing Assistant schema detected; recording the current migration head"
        )
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    database_url = os.environ.get(
        "ASSISTANT_DATABASE_URL",
        "postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator",
    )
    logger.info("Applying Community Assistant database migrations")
    migrate(database_url)
    logger.info("Community Assistant migrations are current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
