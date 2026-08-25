"""Runtime observability bus: bounded metrics + trace timeline + structured log.

One in-process instance is created at app startup and threaded through the
critical lifecycle boundaries (Turn, ActionLoop, Operation, Worker,
Reconciliation).  It never stores prompts, tokens, or full bodies.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .prometheus_metrics import Counter, Histogram, MetricsRegistry
from .trace_store import TraceStore

logger = logging.getLogger(__name__)

# Metric names kept bounded (15-20 core).  Labels are low-cardinality only.
_AGENT_TURN = "agent_turn_total"
_AGENT_TURN_LATENCY = "agent_turn_latency_seconds"
_AGENT_LLM_CALLS = "agent_llm_calls_total"
_AGENT_ACTIONLOOP_ITERATIONS = "agent_actionloop_iterations"
_AGENT_TARGET_RESOLUTION = "agent_target_resolution_total"
_AGENT_CLARIFICATION = "agent_clarification_total"
_AGENT_FASTPATH = "agent_fastpath_total"
_AGENT_OPERATION = "agent_operation_total"
_AGENT_OPERATION_LATENCY = "agent_operation_latency_seconds"
_AGENT_QUEUE_WAIT = "agent_queue_wait_seconds"
_AGENT_RESULT_UNKNOWN = "agent_result_unknown_total"
_AGENT_RECONCILIATION = "agent_reconciliation_total"
_AGENT_VERIFICATION_FAILURE = "agent_verification_failure_total"
_AGENT_WORKER_RECLAIM = "agent_worker_reclaim_total"
_AGENT_OPERATION_RETRY = "agent_operation_retry_total"
_AGENT_TASK = "agent_task_total"
_AGENT_TASK_COMPLETION_LATENCY = "agent_task_completion_latency_seconds"


class RuntimeObservability:
    """Composite observability seam (metrics + trace timeline)."""

    def __init__(self) -> None:
        self.metrics = MetricsRegistry()
        self.traces = TraceStore()

    # ── metric accessors (lazily created, low-cardinality) ──────────────
    def turn_total(self) -> Counter:
        return self.metrics.counter(_AGENT_TURN, "Agent turns", ("outcome",))

    def turn_latency(self) -> Histogram:
        return self.metrics.histogram(_AGENT_TURN_LATENCY, "Agent turn latency seconds")

    def llm_calls(self) -> Counter:
        return self.metrics.counter(_AGENT_LLM_CALLS, "Agent LLM calls")

    def actionloop_iterations(self) -> Histogram:
        return self.metrics.histogram(_AGENT_ACTIONLOOP_ITERATIONS, "ActionLoop iterations")

    def target_resolution(self) -> Counter:
        return self.metrics.counter(
            _AGENT_TARGET_RESOLUTION, "Target resolution", ("status",)
        )

    def clarification(self) -> Counter:
        return self.metrics.counter(_AGENT_CLARIFICATION, "Clarifications")

    def fastpath(self) -> Counter:
        return self.metrics.counter(_AGENT_FASTPATH, "FastPath routing", ("route",))

    def operation(self) -> Counter:
        return self.metrics.counter(
            _AGENT_OPERATION, "Logical operations", ("semantic_action", "outcome")
        )

    def operation_latency(self) -> Histogram:
        return self.metrics.histogram(_AGENT_OPERATION_LATENCY, "Operation latency seconds")

    def queue_wait(self) -> Histogram:
        return self.metrics.histogram(_AGENT_QUEUE_WAIT, "Execution queue wait seconds")

    def result_unknown(self) -> Counter:
        return self.metrics.counter(_AGENT_RESULT_UNKNOWN, "RESULT_UNKNOWN operations")

    def reconciliation(self) -> Counter:
        return self.metrics.counter(
            _AGENT_RECONCILIATION, "Reconciliations", ("outcome",)
        )

    def verification_failure(self) -> Counter:
        return self.metrics.counter(_AGENT_VERIFICATION_FAILURE, "Verification failures")

    def worker_reclaim(self) -> Counter:
        return self.metrics.counter(_AGENT_WORKER_RECLAIM, "Worker lease reclaims")

    def operation_retry(self) -> Counter:
        return self.metrics.counter(
            _AGENT_OPERATION_RETRY, "Operation retries", ("retry_classification",)
        )

    def task(self) -> Counter:
        return self.metrics.counter(_AGENT_TASK, "Agent tasks", ("outcome",))

    def task_completion_latency(self) -> Histogram:
        return self.metrics.histogram(_AGENT_TASK_COMPLETION_LATENCY, "Task completion latency seconds")

    def render_metrics(self) -> str:
        return self.metrics.render()

    # ── trace recording ────────────────────────────────────────────────
    def record_trace(self, stage: str, **span: Any) -> None:
        self.traces.record(stage, **span)

    def structured_log(
        self,
        event: str,
        *,
        level: int = logging.INFO,
        **fields: Any,
    ) -> None:
        """Emit one structured JSON log line with the supplied correlation fields.

        Sensitive values must not be passed here; this only forwards the caller's
        already-sanitised fields.
        """
        logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))


_instance: RuntimeObservability | None = None


def observability() -> RuntimeObservability:
    global _instance
    if _instance is None:
        _instance = RuntimeObservability()
    return _instance


def set_observability(instance: RuntimeObservability) -> None:
    global _instance
    _instance = instance


__all__ = ["RuntimeObservability", "observability", "set_observability"]
