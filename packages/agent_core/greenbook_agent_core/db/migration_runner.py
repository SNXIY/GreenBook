"""Apply Agent database migrations in a deterministic, idempotent order."""

from __future__ import annotations

import re
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
        row[0] for row in (
            await session.execute(
                sa.text("SELECT version FROM assistant_schema_migrations")
            )
        ).all()
    )

    executed: list[str] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = path.name
        if version in applied:
            continue
        sql = path.read_text(encoding="utf-8").strip()
        for statement in _split_sql_statements(sql):
            await session.execute(sa.text(statement))
        await session.execute(
            sa.text(
                "INSERT INTO assistant_schema_migrations (version) VALUES (:version)"
            ),
            {"version": version},
        )
        executed.append(version)
    await session.commit()
    return executed


_DOLLAR_QUOTE = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")


def _split_sql_statements(sql: str) -> list[str]:
    """Split migration SQL without splitting quoted or dollar-quoted text.

    asyncpg prepares one SQL command per ``Session.execute`` call. Migration
    files may contain several idempotent DDL commands, so execute each command
    separately while keeping PostgreSQL string/function literals intact.
    """

    statements: list[str] = []
    buffer: list[str] = []
    quote: str | None = None
    dollar_quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0

    while index < len(sql):
        character = sql[index]
        following = sql[index + 1] if index + 1 < len(sql) else ""

        if line_comment:
            buffer.append(character)
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            buffer.append(character)
            if character == "*" and following == "/":
                buffer.append(following)
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if dollar_quote is not None:
            if sql.startswith(dollar_quote, index):
                buffer.append(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
            else:
                buffer.append(character)
                index += 1
            continue
        if quote is not None:
            buffer.append(character)
            if character == quote:
                if following == quote:
                    buffer.append(following)
                    index += 2
                    continue
                quote = None
            index += 1
            continue

        if character == "-" and following == "-":
            buffer.extend((character, following))
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            buffer.extend((character, following))
            block_comment = True
            index += 2
            continue
        if character in {"'", '"'}:
            buffer.append(character)
            quote = character
            index += 1
            continue
        if character == "$":
            match = _DOLLAR_QUOTE.match(sql, index)
            if match is not None:
                dollar_quote = match.group(0)
                buffer.append(dollar_quote)
                index = match.end()
                continue
        if character == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
        else:
            buffer.append(character)
        index += 1

    statement = "".join(buffer).strip()
    if statement:
        statements.append(statement)
    return statements


__all__ = ["apply_migrations"]
