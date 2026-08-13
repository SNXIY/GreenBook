from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from app.core.config import Settings, get_settings
from app.creator.deployment.migrate import migrate
from app.creator.drafts import sqlalchemy as draft_models  # noqa: F401
from app.creator.evaluation import sqlalchemy as evaluation_models  # noqa: F401
from app.creator.infrastructure.sqlalchemy import CreatorBase
from app.creator.memory import sqlalchemy as memory_models  # noqa: F401
from app.creator.publication import sqlalchemy as publication_models  # noqa: F401
from app.creator.retrieval import sqlalchemy as retrieval_models  # noqa: F401
from app.creator.studio import sqlalchemy as studio_models  # noqa: F401
from app.creator.tools import sqlalchemy as tool_models  # noqa: F401


class CreatorMigrationTests(unittest.TestCase):
    def test_baseline_upgrade_matches_metadata_and_downgrades(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "creator-migrations.db"
            database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
            config = Config(str(project_root / "alembic.ini"))

            with patch.dict(
                os.environ,
                {"GREENBOOK_CREATOR_DATABASE_URL": database_url},
            ):
                get_settings.cache_clear()
                try:
                    command.upgrade(config, "head")
                    with closing(sqlite3.connect(database_path)) as connection:
                        tables = {
                            row[0]
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master " "WHERE type = 'table'"
                            )
                        }
                        revision = connection.execute(
                            "SELECT version_num FROM creator_alembic_version"
                        ).fetchone()

                    self.assertEqual(
                        set(CreatorBase.metadata.tables),
                        tables - {"creator_alembic_version"},
                    )
                    self.assertEqual(revision, ("01f77611bde3",))
                    command.check(config)

                    command.downgrade(config, "base")
                    with closing(sqlite3.connect(database_path)) as connection:
                        remaining = {
                            row[0]
                            for row in connection.execute(
                                "SELECT name FROM sqlite_master "
                                "WHERE type = 'table' AND name LIKE 'creator_%'"
                            )
                        }
                    self.assertEqual(
                        remaining,
                        {"creator_alembic_version"},
                    )
                finally:
                    get_settings.cache_clear()

    def test_deployment_migration_initializes_checkpoint_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            creator_database = root / "creator.db"
            checkpoint_database = root / "checkpoints.db"
            settings = Settings(
                _env_file=None,
                creator_database_url=(
                    f"sqlite+aiosqlite:///{creator_database.as_posix()}"
                ),
                creator_checkpoint_backend="sqlite",
                creator_checkpoint_sqlite_path=str(checkpoint_database),
                creator_checkpoint_auto_setup=False,
            )

            migrate(settings)

            with closing(sqlite3.connect(creator_database)) as connection:
                creator_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            with closing(sqlite3.connect(checkpoint_database)) as connection:
                checkpoint_tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("creator_tasks", creator_tables)
            self.assertEqual(
                checkpoint_tables,
                {"checkpoints", "writes"},
            )

    def test_deployment_migration_stamps_an_existing_unversioned_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            creator_database = root / "creator.db"
            checkpoint_database = root / "checkpoints.db"
            database_url = f"sqlite+aiosqlite:///{creator_database.as_posix()}"
            engine = create_engine(f"sqlite:///{creator_database.as_posix()}")
            CreatorBase.metadata.create_all(engine)
            engine.dispose()
            settings = Settings(
                _env_file=None,
                creator_database_url=database_url,
                creator_checkpoint_backend="sqlite",
                creator_checkpoint_sqlite_path=str(checkpoint_database),
                creator_checkpoint_auto_setup=False,
            )

            migrate(settings)

            with closing(sqlite3.connect(creator_database)) as connection:
                revision = connection.execute(
                    "SELECT version_num FROM creator_alembic_version"
                ).fetchone()
            self.assertEqual(revision, ("01f77611bde3",))


if __name__ == "__main__":
    unittest.main()
