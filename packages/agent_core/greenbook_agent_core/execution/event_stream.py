"""Polling subscription for execution events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from .events import ExecutionEvent
from .models import ExecutionStatus

StreamDisconnect = Callable[[], Awaitable[bool]]
ExecutionStatusGetter = Callable[[], Any]


async def subscribe_execution_events(
    execution_id: str,
    event_store: Any,
    execution_status: ExecutionStatusGetter,
    *,
    is_disconnected: StreamDisconnect | None = None,
    poll_interval: float = 0.1,
    event_types: set[str] | None = None,
) -> AsyncIterator[ExecutionEvent]:
    """Yield new events until the client disconnects or execution is terminal."""
    cursor = 0
    allowed = event_types
    terminal = {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    }
    while True:
        events = event_store.list_events(execution_id)
        pending = events[cursor:]
        cursor = len(events)
        for event in pending:
            if allowed is None or event.event_type.value in allowed:
                yield event

        if is_disconnected is not None and await is_disconnected():
            return
        current = execution_status()
        if current in terminal and cursor >= len(events):
            return
        await asyncio.sleep(poll_interval)


__all__ = ["subscribe_execution_events"]
