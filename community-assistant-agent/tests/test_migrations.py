from __future__ import annotations

import sqlite3
from contextlib import closing

from sqlalchemy import create_engine, text

from app.migrations import MigrationBootstrap, _prepare_version_table


def test_legacy_assistant_version_table_is_renamed(tmp_path) -> None:
    database_path = tmp_path / "assistant.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(64))"))
        connection.execute(
            text(
                "INSERT INTO alembic_version (version_num) "
                "VALUES ('008_four_layer_memory')"
            )
        )
        action = _prepare_version_table(connection)

    assert action is MigrationBootstrap.UPGRADE
    with closing(sqlite3.connect(database_path)) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "assistant_alembic_version" in tables
    assert "alembic_version" not in tables


def test_existing_unversioned_assistant_schema_is_stamped(tmp_path) -> None:
    database_path = tmp_path / "assistant.db"
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE assistant_runs (id VARCHAR(36))"))
        action = _prepare_version_table(connection)

    assert action is MigrationBootstrap.STAMP
