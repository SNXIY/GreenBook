"""Startup checks for the Runtime/Legacy projection database boundary."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

RUNTIME_SCHEMA_MISMATCH = (
    "Runtime projection schema mismatch: "
    "assistant_runs.status must be nullable before enabling Runtime mode."
)


async def is_assistant_runs_status_nullable(session: AsyncSession) -> bool:
    """Return whether the live PostgreSQL assistant_runs.status is nullable."""

    result = await session.execute(
        sa.text(
            """
            SELECT is_nullable
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'assistant_runs'
              AND column_name = 'status'
            """
        )
    )
    return result.scalar_one_or_none() == "YES"


async def verify_runtime_projection_schema(
    session: AsyncSession,
    *,
    runtime_mode: str,
) -> None:
    """Fail startup when Runtime mode points at an incompatible schema."""

    if runtime_mode.strip().lower() == "off":
        return
    if not await is_assistant_runs_status_nullable(session):
        raise RuntimeError(RUNTIME_SCHEMA_MISMATCH)


__all__ = [
    "RUNTIME_SCHEMA_MISMATCH",
    "is_assistant_runs_status_nullable",
    "verify_runtime_projection_schema",
]
