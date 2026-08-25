"""Lightweight in-memory trace timeline for one turn.

Records lifecycle events keyed by trace_id so a developer can reconstruct the
full Turn -> Task -> Operation -> Worker -> Java -> Verification -> Resume path
for a single request.  Never stores prompts, tokens, or full bodies — only
stage names, ids, statuses, and latencies.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class TraceSpan(BaseModel):
    """One lifecycle event in a turn timeline."""

    seq: int = 0
    stage: str = ""
    trace_id: str = ""
    conversation_id: str = ""
    task_id: str = ""
    objective_id: str = ""
    operation_id: str = ""
    execution_id: str = ""
    tool_call_id: str = ""
    semantic_action: str = ""
    status: str = ""
    latency_ms: float | None = None
    error_code: str = ""
    at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class TraceTimeline:
    """One trace's ordered spans."""

    def __init__(self, trace_id: str) -> None:
        self.trace_id = trace_id
        self._spans: list[TraceSpan] = []
        self._lock = threading.RLock()
        self._seq = 0

    def add(self, span: TraceSpan) -> None:
        with self._lock:
            self._seq += 1
            self._spans.append(span.model_copy(update={"seq": self._seq}))

    def spans(self) -> list[TraceSpan]:
        with self._lock:
            return [s.model_copy(deep=True) for s in sorted(self._spans, key=lambda s: s.seq)]

    def __len__(self) -> int:
        return len(self._spans)


class TraceStore:
    """In-memory store of trace timelines (dev/debug only, non-durable)."""

    def __init__(self, *, max_traces: int = 2000) -> None:
        self._traces: dict[str, TraceTimeline] = {}
        self._lock = threading.RLock()
        self._max_traces = max(200, max_traces)

    def timeline(self, trace_id: str) -> TraceTimeline:
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None:
                trace = TraceTimeline(trace_id)
                self._traces[trace_id] = trace
                if len(self._traces) > self._max_traces:
                    oldest = next(iter(self._traces))
                    self._traces.pop(oldest, None)
            return trace

    def record(
        self,
        stage: str,
        *,
        trace_id: str,
        conversation_id: str = "",
        task_id: str = "",
        objective_id: str = "",
        operation_id: str = "",
        execution_id: str = "",
        tool_call_id: str = "",
        semantic_action: str = "",
        status: str = "",
        latency_ms: float | None = None,
        error_code: str = "",
    ) -> None:
        if not trace_id:
            return
        span = TraceSpan(
            stage=stage,
            trace_id=trace_id,
            conversation_id=conversation_id,
            task_id=task_id,
            objective_id=objective_id,
            operation_id=operation_id,
            execution_id=execution_id,
            tool_call_id=tool_call_id,
            semantic_action=semantic_action,
            status=status,
            latency_ms=latency_ms,
            error_code=error_code,
        )
        self.timeline(trace_id).add(span)

    def get(self, trace_id: str) -> TraceTimeline | None:
        with self._lock:
            return self._traces.get(trace_id)

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()


__all__ = ["TraceSpan", "TraceStore", "TraceTimeline"]
