"""Small Runtime metrics abstraction with a deterministic memory backend."""

from __future__ import annotations

from threading import RLock
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .context import TraceContext


class RuntimeMetricsSnapshot(BaseModel):
    """Counter and duration snapshot for one MetricsCollector instance."""

    model_config = ConfigDict(frozen=True)

    execution_total: int = 0
    execution_success: int = 0
    execution_failed: int = 0
    execution_duration_ms: float = 0.0

    step_total: int = 0
    step_failure: int = 0
    step_latency_ms: float = 0.0

    tool_invocation_count: int = 0
    tool_error_count: int = 0
    tool_latency_ms: float = 0.0

    retry_count: int = 0
    retry_success: int = 0

    reconciliation_unknown: int = 0
    reconciliation_resolved: int = 0

    @property
    def execution_success_rate(self) -> float:
        return self.execution_success / self.execution_total if self.execution_total else 0.0

    @property
    def step_failure_rate(self) -> float:
        return self.step_failure / self.step_total if self.step_total else 0.0

    @property
    def tool_error_rate(self) -> float:
        return self.tool_error_count / self.tool_invocation_count if self.tool_invocation_count else 0.0

    @property
    def average_execution_duration_ms(self) -> float:
        return self.execution_duration_ms / self.execution_total if self.execution_total else 0.0

    @property
    def average_step_latency_ms(self) -> float:
        return self.step_latency_ms / self.step_total if self.step_total else 0.0

    @property
    def average_tool_latency_ms(self) -> float:
        return self.tool_latency_ms / self.tool_invocation_count if self.tool_invocation_count else 0.0

    def as_dict(self) -> dict[str, object]:
        """Expose grouped names for API/read-model consumers."""

        return {
            "execution": {
                "total": self.execution_total,
                "success": self.execution_success,
                "failed": self.execution_failed,
                "duration_ms": self.execution_duration_ms,
                "success_rate": self.execution_success_rate,
            },
            "step": {
                "total": self.step_total,
                "failure": self.step_failure,
                "latency_ms": self.step_latency_ms,
                "failure_rate": self.step_failure_rate,
            },
            "tool": {
                "invocation_count": self.tool_invocation_count,
                "error_count": self.tool_error_count,
                "latency_ms": self.tool_latency_ms,
                "error_rate": self.tool_error_rate,
            },
            "retry": {
                "count": self.retry_count,
                "success": self.retry_success,
            },
            "reconciliation": {
                "unknown": self.reconciliation_unknown,
                "resolved": self.reconciliation_resolved,
            },
        }


class MetricsCollector(Protocol):
    """Instrumentation seam consumed by Runtime execution boundaries."""

    def record_execution(
        self,
        *,
        status: str,
        duration_ms: float,
        context: TraceContext | None = None,
    ) -> None: ...

    def record_step(
        self,
        *,
        status: str,
        latency_ms: float,
        context: TraceContext | None = None,
    ) -> None: ...

    def record_tool(
        self,
        *,
        status: str,
        latency_ms: float,
        context: TraceContext | None = None,
    ) -> None: ...

    def record_retry(
        self,
        *,
        success: bool = False,
        context: TraceContext | None = None,
    ) -> None: ...

    def record_reconciliation(
        self,
        *,
        status: str,
        context: TraceContext | None = None,
    ) -> None: ...

    def snapshot(self) -> RuntimeMetricsSnapshot: ...


class MemoryMetricsCollector:
    """Thread-safe in-memory implementation for local runtime and tests."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._values: dict[str, int | float] = {
            field: 0
            for field in RuntimeMetricsSnapshot.model_fields
        }

    def record_execution(
        self,
        *,
        status: str,
        duration_ms: float,
        context: TraceContext | None = None,
    ) -> None:
        del context
        with self._lock:
            self._increment("execution_total")
            self._add("execution_duration_ms", duration_ms)
            if str(status).upper() in {"COMPLETED", "SUCCESS", "SUCCEEDED"}:
                self._increment("execution_success")
            else:
                self._increment("execution_failed")

    def record_step(
        self,
        *,
        status: str,
        latency_ms: float,
        context: TraceContext | None = None,
    ) -> None:
        del context
        with self._lock:
            self._increment("step_total")
            self._add("step_latency_ms", latency_ms)
            if str(status).upper() in {
                "FAILED",
                "FAILED_RETRYABLE",
                "TIMEOUT",
                "ERROR",
            }:
                self._increment("step_failure")

    def record_tool(
        self,
        *,
        status: str,
        latency_ms: float,
        context: TraceContext | None = None,
    ) -> None:
        del context
        with self._lock:
            self._increment("tool_invocation_count")
            self._add("tool_latency_ms", latency_ms)
            if str(status).upper() not in {"COMPLETED", "SUCCESS", "SUCCEEDED", "PENDING"}:
                self._increment("tool_error_count")

    def record_retry(
        self,
        *,
        success: bool = False,
        context: TraceContext | None = None,
    ) -> None:
        del context
        with self._lock:
            if success:
                self._increment("retry_success")
            else:
                self._increment("retry_count")

    def record_reconciliation(
        self,
        *,
        status: str,
        context: TraceContext | None = None,
    ) -> None:
        del context
        with self._lock:
            if str(status).upper() == "UNKNOWN":
                self._increment("reconciliation_unknown")
            else:
                self._increment("reconciliation_resolved")

    def snapshot(self) -> RuntimeMetricsSnapshot:
        with self._lock:
            return RuntimeMetricsSnapshot.model_validate(dict(self._values))

    def reset(self) -> None:
        with self._lock:
            for field in self._values:
                self._values[field] = 0

    def _increment(self, name: str) -> None:
        self._values[name] = int(self._values[name]) + 1

    def _add(self, name: str, value: float) -> None:
        self._values[name] = float(self._values[name]) + max(0.0, float(value))


__all__ = [
    "MemoryMetricsCollector",
    "MetricsCollector",
    "RuntimeMetricsSnapshot",
]
