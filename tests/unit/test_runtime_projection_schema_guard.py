"""Phase 11.6-D8.6 Runtime projection schema guard tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from greenbook_assistant_core.db.schema_guard import (
    RUNTIME_SCHEMA_MISMATCH,
    verify_runtime_projection_schema,
)


def _session(*, nullable: bool) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "YES" if nullable else "NO"
    session.execute.return_value = result
    return session


@pytest.mark.asyncio
async def test_runtime_mode_with_nullable_status_passes() -> None:
    await verify_runtime_projection_schema(
        _session(nullable=True), runtime_mode="on"
    )


@pytest.mark.asyncio
async def test_runtime_mode_with_not_null_status_fails_startup() -> None:
    with pytest.raises(RuntimeError, match=RUNTIME_SCHEMA_MISMATCH):
        await verify_runtime_projection_schema(
            _session(nullable=False), runtime_mode="on"
        )


@pytest.mark.asyncio
async def test_legacy_mode_allows_old_schema() -> None:
    session = _session(nullable=False)
    await verify_runtime_projection_schema(session, runtime_mode="off")
    session.execute.assert_not_awaited()
