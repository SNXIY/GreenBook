"""SQLAlchemy async engine and session factory for GreenBook Agent."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_ENV_PREFIXES = (
    "GREENBOOK_AGENT_DATABASE_URL",
    "GREENBOOK_AGENT_DB_URL",
    "GREENBOOK_DB_URL",
)
_DEFAULT = (
    "postgresql+asyncpg://mindflow:mindflow@127.0.0.1:25432/mindflow_creator"
)


def _db_url() -> str:
    for name in _ENV_PREFIXES:
        value = os.getenv(name)
        if value:
            return value
    return os.getenv("DATABASE_URL", _DEFAULT)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(db_url: str | None = None) -> AsyncEngine:
    """Create (or return the cached) async engine."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    url = db_url or _db_url()
    _engine = create_async_engine(url, pool_size=5, max_overflow=10, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory, auto-creating the engine if needed."""
    if _session_factory is None:
        create_engine()
    return _session_factory  # type: ignore[return-value]


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


@asynccontextmanager
async def session_ctx() -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped session and rollback failed transactions.

    ``AsyncSession`` closes its connection when leaving the context.  The
    explicit rollback is important for exceptions because it clears a failed
    transaction before the connection is returned to asyncpg's pool.
    """
    factory = session_factory()
    async with factory() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
