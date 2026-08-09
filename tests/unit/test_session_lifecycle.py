"""Request-scoped AsyncSession lifecycle tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")

from apps.assistant_api.greenbook_assistant_api.main import (
    _close_request_db_session,
    _db_session_lifecycle,
)


def _request_with_session(session: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(_assistant_db_session=session),
    )


@pytest.mark.asyncio
async def test_request_session_rolls_back_and_closes_on_exception() -> None:
    session = AsyncMock()
    request = _request_with_session(session)

    await _close_request_db_session(request, rollback=True)

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()
    assert request.state._assistant_db_session is None


@pytest.mark.asyncio
async def test_middleware_closes_session_after_successful_response() -> None:
    session = AsyncMock()
    request = _request_with_session(session)

    async def call_next(_request):
        return SimpleNamespace(status_code=200)

    response = await _db_session_lifecycle(request, call_next)

    assert response.status_code == 200
    session.rollback.assert_not_awaited()
    session.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_middleware_rolls_back_handled_error_response() -> None:
    session = AsyncMock()
    request = _request_with_session(session)

    async def call_next(_request):
        return SimpleNamespace(status_code=502)

    await _db_session_lifecycle(request, call_next)

    session.rollback.assert_awaited_once()
    session.close.assert_awaited_once()
