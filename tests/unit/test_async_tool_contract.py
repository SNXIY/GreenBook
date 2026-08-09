"""Runtime long-tool contract regression tests."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from greenbook_assistant_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)
from greenbook_assistant_core.execution.runtime.ledger import InvocationStatus
from greenbook_assistant_core.execution.runtime.tool_runtime import (
    AsyncTaskHandle,
    ToolRuntime,
)


def _context(timeout_seconds: float = 0.1) -> ToolInvocationContext:
    return ToolInvocationContext.build(
        task_id="task-long",
        execution_id="execution-long",
        step_id="generate",
        capability="GENERATE_CONTENT",
        tool_name="content.create_draft",
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.asyncio
async def test_long_tool_acknowledges_without_timeout() -> None:
    completed: list[dict[str, Any]] = []

    async def handler(_name: str, _args: dict[str, Any]) -> AsyncTaskHandle:
        async def finish() -> dict[str, Any]:
            await asyncio.sleep(0.03)
            return {"ok": True, "data": {"draft_id": "draft-1"}}

        return AsyncTaskHandle(
            task_id="creator-task-1",
            awaitable=finish(),
        )

    async def on_complete(_ctx: ToolInvocationContext, result: Any) -> None:
        completed.append(dict(result.data))

    runtime = ToolRuntime(handler, on_async_complete=on_complete)
    result = await runtime.invoke(_context())

    assert result.pending is True
    assert result.status == "RUNNING"
    assert result.data == {"task_id": "creator-task-1", "status": "RUNNING"}

    await asyncio.sleep(0.06)
    assert completed == [{"draft_id": "draft-1"}]
    replayed = await runtime.invoke(_context())
    assert replayed.ok is True
    assert replayed.replayed is True
    assert replayed.data == {"draft_id": "draft-1"}
    entry = runtime.ledger.find_by_id(result.invocation_id)
    assert entry is not None
    assert entry.status == InvocationStatus.COMPLETED


@pytest.mark.asyncio
async def test_async_tool_failure_is_reported_after_ack() -> None:
    async def handler(_name: str, _args: dict[str, Any]) -> AsyncTaskHandle:
        async def finish() -> dict[str, Any]:
            await asyncio.sleep(0.01)
            return {
                "ok": False,
                "code": "CREATOR_UNAVAILABLE",
                "user_message": "Creator is unavailable",
            }

        return AsyncTaskHandle("creator-task-2", finish())

    runtime = ToolRuntime(handler)
    result = await runtime.invoke(_context())
    assert result.pending is True

    await asyncio.sleep(0.03)
    entry = runtime.ledger.find_by_id(result.invocation_id)
    assert entry is not None
    assert entry.status == InvocationStatus.FAILED
    assert entry.error_code == "CREATOR_UNAVAILABLE"


@pytest.mark.asyncio
async def test_async_tool_deadline_is_reported_after_ack() -> None:
    async def handler(_name: str, _args: dict[str, Any]) -> AsyncTaskHandle:
        async def finish() -> dict[str, Any]:
            await asyncio.sleep(0.05)
            return {"ok": True, "data": {"draft_id": "too-late"}}

        return AsyncTaskHandle("creator-task-timeout", finish())

    runtime = ToolRuntime(handler)
    result = await runtime.invoke(_context(timeout_seconds=0.01))
    assert result.pending is True

    await asyncio.sleep(0.03)
    entry = runtime.ledger.find_by_id(result.invocation_id)
    assert entry is not None
    assert entry.status == InvocationStatus.TIMEOUT
    assert entry.error_code == "TIMEOUT"
