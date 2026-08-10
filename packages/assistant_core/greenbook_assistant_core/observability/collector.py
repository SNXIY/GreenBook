"""TraceCollector — event bus + storage for execution traces.

Phase 4.4: in-memory.  Plug into ExecutionWorker / ToolRuntime /
ArtifactStore to emit events at key lifecycle points.
"""

from __future__ import annotations

from .context import TraceContext
from .models import EventType, Trace, TraceEvent


class TraceCollector:
    """Collect and query TraceEvents across executions."""

    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}

    # ── factory ──────────────────────────────────────────────────

    def create_trace(
        self,
        trace_id: str = "",
        task_id: str = "",
        execution_id: str = "",
        user_id: str = "",
        trace_context: TraceContext | None = None,
    ) -> Trace:
        trace = Trace(
            trace_id=trace_id,
            task_id=task_id,
            execution_id=execution_id,
            user_id=user_id,
            trace_context=trace_context,
        )
        self._traces[trace.trace_id] = trace
        return trace

    def get_or_create(
        self,
        trace_id: str,
        task_id: str = "",
        execution_id: str = "",
        trace_context: TraceContext | None = None,
    ) -> Trace:
        if trace_id in self._traces:
            trace = self._traces[trace_id]
            if trace_context is not None:
                trace.trace_context = trace_context
            return trace
        return self.create_trace(
            trace_id=trace_id,
            task_id=task_id,
            execution_id=execution_id,
            trace_context=trace_context,
        )

    # ── emit ─────────────────────────────────────────────────────

    def emit(self, event: TraceEvent) -> TraceEvent:
        """Record *event* into its owning trace (keyed by trace_id)."""
        trace = self._traces.get(event.trace_id)
        if trace is None:
            trace = self.create_trace(trace_id=event.trace_id)
        if event.trace_context is None and trace.trace_context is not None:
            event.trace_context = trace.trace_context
        trace.events.append(event)
        return event

    def bind_context(self, trace_id: str, context: TraceContext) -> Trace:
        """Apply execution-scoped identifiers to existing trace events."""

        trace = self.get_or_create(trace_id, trace_context=context)
        trace.trace_context = context
        for event in trace.events:
            previous = event.trace_context
            event.trace_context = context.with_updates(
                step_id=(previous.step_id if previous is not None else event.step_id) or None,
                invocation_id=(previous.invocation_id if previous is not None else None) or None,
                tool_call_id=(previous.tool_call_id if previous is not None else None) or None,
                operation_id=(previous.operation_id if previous is not None else None) or None,
            )
        return trace

    def emit_event(
        self,
        trace_id: str,
        event_type: EventType,
        *,
        execution_id: str = "",
        step_id: str = "",
        tool_name: str = "",
        capability: str = "",
        artifact_type: str = "",
        payload: dict | None = None,
        trace_context: TraceContext | None = None,
    ) -> TraceEvent:
        """Convenience: create + emit a TraceEvent in one call."""
        return self.emit(TraceEvent(
            trace_id=trace_id,
            event_type=event_type,
            execution_id=execution_id,
            step_id=step_id,
            tool_name=tool_name,
            capability=capability,
            artifact_type=artifact_type,
            payload=payload or {},
            trace_context=trace_context,
        ))

    # ── queries ──────────────────────────────────────────────────

    def find_trace(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)

    def find_by_execution(self, execution_id: str) -> list[TraceEvent]:
        events: list[TraceEvent] = []
        for trace in self._traces.values():
            for evt in trace.events:
                if evt.execution_id == execution_id:
                    events.append(evt)
        events.sort(key=lambda e: e.timestamp)
        return events

    def timeline(self, trace_id: str) -> list[TraceEvent]:
        trace = self._traces.get(trace_id)
        if trace is None:
            return []
        return trace.timeline()

    def count(self) -> int:
        return len(self._traces)

    def clear(self) -> None:
        self._traces.clear()
