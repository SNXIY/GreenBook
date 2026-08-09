"""In-memory append-only execution event store."""

from __future__ import annotations

from .events import ExecutionEvent


class ExecutionEventStore:
    def __init__(self) -> None:
        self._events: dict[str, list[ExecutionEvent]] = {}

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        self._events.setdefault(event.execution_id, []).append(event)
        return event

    def list_events(self, execution_id: str) -> list[ExecutionEvent]:
        return [event.model_copy(deep=True) for event in self._events.get(execution_id, [])]

    def clear(self, execution_id: str) -> None:
        self._events.pop(execution_id, None)


EventStore = ExecutionEventStore

__all__ = ["ExecutionEventStore", "EventStore"]
