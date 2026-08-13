"""PostgreSQL migration execution contract tests."""

from __future__ import annotations

from pathlib import Path

from greenbook_agent_core.db.migration_runner import _split_sql_statements


def test_migration_sql_is_split_for_asyncpg_prepared_statements() -> None:
    root = Path(__file__).parents[2]
    migration = (
        root
        / "packages"
        / "agent_core"
        / "greenbook_agent_core"
        / "db"
        / "migrations"
        / "008_context_durable_memory.sql"
    )

    statements = _split_sql_statements(migration.read_text(encoding="utf-8"))

    assert len(statements) == 3
    assert statements[0].lstrip().startswith("-- Phase 5")
    assert statements[1].startswith("CREATE INDEX")
    assert statements[2].startswith("CREATE INDEX")


def test_migration_splitter_ignores_semicolons_in_literals_and_comments() -> None:
    statements = _split_sql_statements(
        "CREATE TABLE sample (value TEXT DEFAULT 'a;b');"
        "-- comment; should not split\n"
        "CREATE FUNCTION sample_fn() RETURNS void AS $$ BEGIN RAISE NOTICE 'x;y'; END; $$ LANGUAGE plpgsql;"
    )

    assert len(statements) == 2
    assert "'a;b'" in statements[0]
    assert "'x;y'" in statements[1]
