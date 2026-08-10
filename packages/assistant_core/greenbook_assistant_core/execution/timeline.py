"""Read-only execution timeline assembled from canonical Runtime records."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from greenbook_assistant_core.observability.context import TraceContext

from .events import EventType, ExecutionEvent
from .evidence import ExecutionEvidence
from .operation_tracking import ExternalOperationRecord


class TimelineItemKind(StrEnum):
    """Stable read-model categories for Runtime timeline consumers."""

    EVENT = "event"
    STEP = "step"
    TOOL = "tool"
    RETRY = "retry"
    RECONCILIATION = "reconciliation"
    EXTERNAL_OPERATION = "external_operation"


class ExecutionTimelineItem(BaseModel):
    """One chronological fact in an execution timeline."""

    model_config = ConfigDict(frozen=True)

    item_id: str
    kind: TimelineItemKind
    source: str
    timestamp: str
    execution_id: str
    event_id: str | None = None
    event_type: str | None = None
    step_id: str | None = None
    invocation_id: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    operation_id: str | None = None
    external_operation_id: str | None = None
    receipt_id: str | None = None
    operation_status: str | None = None
    trace_context: TraceContext | None = None
    evidence: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ExecutionTimeline(BaseModel):
    """Read-only timeline response for one canonical execution."""

    model_config = ConfigDict(frozen=True)

    execution_id: str
    items: list[ExecutionTimelineItem] = Field(default_factory=list)


class _EventStore(Protocol):
    def list_events(self, execution_id: str) -> list[ExecutionEvent]: ...


class _OperationStore(Protocol):
    def find_by_execution_id(self, execution_id: str) -> list[ExternalOperationRecord]: ...


_RETRY_EVENTS = frozenset({
    EventType.STEP_RETRY_REQUESTED,
    EventType.STEP_RETRY_DENIED,
    EventType.STEP_RETRY_STARTED,
    EventType.STEP_RETRY_COMPLETED,
    EventType.STEP_RETRY_EXHAUSTED,
})
_RECONCILIATION_EVENTS = frozenset({
    EventType.STEP_RECONCILIATION_SUCCEEDED,
    EventType.STEP_RECONCILIATION_FAILED,
    EventType.EXECUTION_RECONCILIATION_REQUIRED,
})
_EXECUTION_EVENTS = frozenset({
    EventType.EXECUTION_CREATED,
    EventType.EXECUTION_STARTED,
    EventType.EXECUTION_PAUSED,
    EventType.EXECUTION_RESUMED,
    EventType.EXECUTION_COMPLETED,
    EventType.EXECUTION_FAILED,
    EventType.EXECUTION_CANCELLED,
})


class ExecutionTimelineService:
    """Assemble a timeline without changing the Runtime source of truth.

    Canonical ``ExecutionEvent`` rows remain the primary timeline facts.  An
    optional operation store contributes the latest external-operation
    snapshot as a separate read-model item so reconciliation state is visible
    even when no new execution event was emitted.
    """

    def __init__(
        self,
        event_store: _EventStore,
        operation_store: _OperationStore | None = None,
    ) -> None:
        self._events = event_store
        self._operations = operation_store

    def build(self, execution_id: str) -> ExecutionTimeline:
        items: list[ExecutionTimelineItem] = [
            self._event_item(event)
            for event in self._events.list_events(execution_id)
        ]
        if self._operations is not None:
            items.extend(
                self._operation_item(record)
                for record in self._operations.find_by_execution_id(execution_id)
            )
        items.sort(key=lambda item: (_timestamp_key(item.timestamp), item.item_id))
        return ExecutionTimeline(execution_id=execution_id, items=items)

    def _event_item(self, event: ExecutionEvent) -> ExecutionTimelineItem:
        payload = dict(event.payload or {})
        context = event.trace_context or _context_from(payload)
        evidence = _evidence_from(payload)
        step_id = event.step_id or _value(evidence, "step_id") or _value(context, "step_id")
        invocation_id = _value(evidence, "invocation_id") or _value(context, "invocation_id")
        tool_call_id = _value(evidence, "tool_call_id") or _value(context, "tool_call_id")
        operation_id = (
            _string(payload.get("operation_id"))
            or _value(evidence, "operation_id")
            or _value(context, "operation_id")
        )
        tool_name = _string(payload.get("tool_name"))
        kind = self._kind_for_event(event.event_type, payload, step_id, tool_name)
        return ExecutionTimelineItem(
            item_id=event.event_id,
            kind=kind,
            source="execution_event",
            timestamp=event.timestamp,
            execution_id=event.execution_id,
            event_id=event.event_id,
            event_type=event.event_type.value,
            step_id=step_id,
            invocation_id=invocation_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            operation_id=operation_id,
            external_operation_id=_value(evidence, "external_operation_id"),
            receipt_id=_value(evidence, "receipt_id"),
            trace_context=context,
            evidence=evidence,
            payload=payload,
        )

    @staticmethod
    def _kind_for_event(
        event_type: EventType,
        payload: Mapping[str, Any],
        step_id: str | None,
        tool_name: str | None,
    ) -> TimelineItemKind:
        if event_type in _RETRY_EVENTS:
            return TimelineItemKind.RETRY
        if event_type in _RECONCILIATION_EVENTS:
            return TimelineItemKind.RECONCILIATION
        if event_type in _EXECUTION_EVENTS:
            return TimelineItemKind.EVENT
        if tool_name or "evidence" in payload:
            return TimelineItemKind.TOOL
        if step_id or event_type.value.startswith("STEP_"):
            return TimelineItemKind.STEP
        return TimelineItemKind.EVENT

    @staticmethod
    def _operation_item(record: ExternalOperationRecord) -> ExecutionTimelineItem:
        evidence = (
            record.evidence.model_dump(mode="json")
            if record.evidence is not None
            else None
        )
        context = _context_from_evidence(record)
        return ExecutionTimelineItem(
            item_id=f"operation:{record.operation_id}",
            kind=TimelineItemKind.EXTERNAL_OPERATION,
            source="external_operation",
            timestamp=record.updated_at,
            execution_id=record.execution_id,
            step_id=record.step_id or _value(context, "step_id"),
            invocation_id=_value(record.evidence, "invocation_id"),
            tool_call_id=_value(record.evidence, "tool_call_id"),
            tool_name=record.tool_name or None,
            operation_id=record.operation_id,
            external_operation_id=record.external_operation_id,
            receipt_id=record.receipt_id,
            operation_status=record.status.value,
            trace_context=context,
            evidence=evidence,
            payload={
                "operation_id": record.operation_id,
                "status": record.status.value,
                "external_operation_id": record.external_operation_id,
                "receipt_id": record.receipt_id,
            },
        )


def _context_from(payload: Mapping[str, Any]) -> TraceContext | None:
    value = payload.get("trace_context")
    if not isinstance(value, Mapping):
        return None
    try:
        return TraceContext.model_validate(value)
    except (TypeError, ValueError):
        return None


def _context_from_evidence(record: ExternalOperationRecord) -> TraceContext | None:
    evidence = record.evidence
    if evidence is None and not record.execution_id:
        return None
    return TraceContext(
        trace_id=evidence.trace_id if evidence is not None and evidence.trace_id else "",
        execution_id=record.execution_id,
        step_id=record.step_id,
        invocation_id=evidence.invocation_id if evidence is not None else "",
        tool_call_id=evidence.tool_call_id if evidence is not None else "",
        operation_id=record.operation_id,
    )


def _evidence_from(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    value = payload.get("evidence")
    if not isinstance(value, Mapping):
        return None
    try:
        return ExecutionEvidence.model_validate(value).model_dump(mode="json")
    except (TypeError, ValueError):
        return dict(value)


def _value(value: Any, key: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw = value.get(key)
    else:
        raw = getattr(value, key, None)
    return _string(raw)


def _string(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _timestamp_key(value: str) -> tuple[int, str]:
    try:
        return (0, datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat())
    except (TypeError, ValueError):
        return (1, value)


__all__ = [
    "ExecutionTimeline",
    "ExecutionTimelineItem",
    "ExecutionTimelineService",
    "TimelineItemKind",
]
