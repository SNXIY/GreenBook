"""PostgreSQL smoke coverage for the Runtime projection migration."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.integration
@pytest.mark.asyncio
async def test_runtime_projection_insert_keeps_status_null() -> None:
    database_url = os.getenv("GREENBOOK_AGENT_PROJECTION_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("GREENBOOK_AGENT_PROJECTION_TEST_DATABASE_URL is not configured")
    if not database_url.startswith("postgresql+"):
        pytest.fail("GREENBOOK_AGENT_PROJECTION_TEST_DATABASE_URL must use a PostgreSQL async driver")

    engine = create_async_engine(database_url)
    conversation_id = uuid4()
    run_id = uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                sa.text(
                    """
                    INSERT INTO assistant_conversations
                        (conversation_id, user_id, tenant_id)
                    VALUES (:conversation_id, :user_id, :tenant_id)
                    """
                ),
                {
                    "conversation_id": conversation_id,
                    "user_id": "projection-smoke-user",
                    "tenant_id": "projection-smoke-tenant",
                },
            )
            table = sa.table(
                "assistant_runs",
                sa.column("run_id"),
                sa.column("conversation_id"),
                sa.column("user_id"),
                sa.column("tenant_id"),
                sa.column("content"),
                sa.column("trace_id"),
                sa.column("status"),
            )
            await connection.execute(
                sa.insert(table).values(
                    run_id=run_id,
                    conversation_id=conversation_id,
                    user_id="projection-smoke-user",
                    tenant_id="projection-smoke-tenant",
                    content="runtime metadata",
                    trace_id="projection-smoke-trace",
                )
            )
            status = await connection.scalar(
                sa.text("SELECT status FROM assistant_runs WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
            assert status is None
    finally:
        await engine.dispose()
