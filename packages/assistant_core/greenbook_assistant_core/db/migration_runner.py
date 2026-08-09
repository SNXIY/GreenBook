"""Apply Assistant database migrations in a deterministic, idempotent order."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


_MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_VERSION_TABLE = "assistant_schema_migrations"


async def apply_migrations(session: AsyncSession) -> list[str]:
    """Apply unapplied SQL migrations and return their version names.

    The table is deliberately separate from ``assistant_runs``.  Migration
    application happens after ``create_all`` so existing databases are altered
    without relying on SQLAlchemy metadata reconciliation.
    """

    await session.execute(sa.text(
        """
        CREATE TABLE IF NOT EXISTS assistant_schema_migrations (
            version VARCHAR(255) PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    ))
    applied = set(
        (row[0] for row in (
            await session.execute(
                sa.text("SELECT version FROM assistant_schema_migrations")
            )
        ).all())
    )

    executed: list[str] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8").strip()
        if sql:
            await session.execute(sa.text(sql))
        await session.execute(
            sa.text(
                "INSERT INTO assistant_schema_migrations (version) VALUES (:version)"
            ),
            {"version": version},
        )
        executed.append(version)
    await session.commit()
    return executed


__all__ = ["apply_migrations"]
