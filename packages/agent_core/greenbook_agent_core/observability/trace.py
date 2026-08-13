"""AgentTrace — integration seam for emitting lifecycle events.

Phase 4.4: standalone helper.  Inject into ExecutionWorker,
ArtifactStore, and ToolRuntime to produce a unified trace.
"""

from __future__ import annotations

from greenbook_agent_core.artifact.models import Artifact
from greenbook_agent_core.execution.invocation import ExecutionResult
from greenbook_agent_core.execution.models import StepExecution

from .collector import TraceCollector
from .context import TraceContext
from .models import EventType, TraceEvent


class AgentTrace:
    """Thin wrapper around TraceCollector with type-safe emit methods.

    Usage in worker::

        trace = AgentTrace(collector, trace_id="t-1", execution_id="e-1")
        trace.step_started(step)
        ...
        trace.execution_completed()
    """

    def __init__(
        self,
        collector: TraceCollector,
        *,
        trace_id: str = "",
        execution_id: str = "",
        task_id: str = "",
        user_id: str = "",
        trace_context: TraceContext | None = None,
    ) -> None:
        self._c = collector
        self.context = trace_context or TraceContext(
            trace_id=trace_id,
            execution_id=execution_id,
            task_id=task_id,
        )
        self.trace_id = self.context.trace_id
        self.execution_id = self.context.execution_id
        self.task_id = self.context.task_id
        # Ensure trace exists
        collector.get_or_create(
            self.trace_id,
            task_id=self.task_id,
            execution_id=self.execution_id,
            trace_context=self.context,
        )

    def bind_context(self, context: TraceContext) -> None:
        """Bind execution-scoped identifiers after the Execution is created."""

        self.context = context
        self.trace_id = context.trace_id
        self.execution_id = context.execution_id
        self.task_id = context.task_id
        self._c.bind_context(context.trace_id, context)

    # ── lifecycle events ─────────────────────────────────────────

    def task_created(self, goal: str = "", category: str = "") -> TraceEvent:
        return self._emit(EventType.TASK_CREATED,
                          payload={"goal": goal, "goal_category": category})

    def plan_created(self, plan_source: str = "", step_count: int = 0) -> TraceEvent:
        return self._emit(EventType.PLAN_CREATED,
                          payload={"plan_source": plan_source, "step_count": step_count})

    def execution_started(self) -> TraceEvent:
        return self._emit(EventType.EXECUTION_STARTED)

    def execution_completed(self) -> TraceEvent:
        return self._emit(EventType.EXECUTION_COMPLETED)

    def execution_failed(self, error: str = "") -> TraceEvent:
        return self._emit(EventType.EXECUTION_FAILED, payload={"error": error})

    # ── step events ──────────────────────────────────────────────

    def step_started(self, step: StepExecution) -> TraceEvent:
        return self._emit(
            EventType.STEP_STARTED,
            step_id=step.step_id,
            capability=step.capability,
        )

    def step_completed(self, step: StepExecution) -> TraceEvent:
        return self._emit(
            EventType.STEP_COMPLETED,
            step_id=step.step_id,
            capability=step.capability,
        )

    def step_failed(self, step: StepExecution, error: str = "") -> TraceEvent:
        return self._emit(
            EventType.STEP_FAILED,
            step_id=step.step_id,
            capability=step.capability,
            payload={"error": error, "status": step.status.value},
        )

    def approval_required(self, step: StepExecution) -> TraceEvent:
        return self._emit(
            EventType.APPROVAL_REQUIRED,
            step_id=step.step_id,
            capability=step.capability,
        )

    # ── tool events ──────────────────────────────────────────────

    def tool_invoked(
        self, step: StepExecution, tool_name: str,
    ) -> TraceEvent:
        return self._emit(
            EventType.TOOL_INVOKED,
            step_id=step.step_id,
            capability=step.capability,
            tool_name=tool_name,
        )

    def tool_completed(
        self, step: StepExecution, result: ExecutionResult,
    ) -> TraceEvent:
        return self._emit(
            EventType.TOOL_COMPLETED,
            step_id=step.step_id,
            capability=step.capability,
            tool_name=result.tool_name,
            payload={"ok": result.ok},
        )

    def tool_failed(
        self, step: StepExecution, tool_name: str, error: str,
    ) -> TraceEvent:
        return self._emit(
            EventType.TOOL_FAILED,
            step_id=step.step_id,
            capability=step.capability,
            tool_name=tool_name,
            payload={"error": error},
        )

    # ── artifact events ──────────────────────────────────────────

    def artifact_created(
        self, step: StepExecution, artifact: Artifact,
    ) -> TraceEvent:
        return self._emit(
            EventType.ARTIFACT_CREATED,
            step_id=step.step_id,
            capability=step.capability,
            artifact_type=artifact.artifact_type,
            payload={
                "artifact_id": artifact.artifact_id,
                "resource_id": artifact.resource_id,
                "summary": artifact.summary,
            },
        )

    # ── group events ──────────────────────────────────────────────

    def group_created(self, group_id: str = "", sub_count: int = 0) -> TraceEvent:
        return self._emit(EventType.GROUP_CREATED,
                          payload={"group_id": group_id,
                                   "sub_task_count": sub_count})

    def sub_task_started(self, sub_index: int, user_message: str = "") -> TraceEvent:
        return self._emit(EventType.SUB_TASK_STARTED,
                          payload={"sub_index": sub_index,
                                   "user_message": user_message[:200]})

    def sub_task_completed(self, sub_index: int, task_id: str = "") -> TraceEvent:
        return self._emit(EventType.SUB_TASK_COMPLETED,
                          payload={"sub_index": sub_index, "task_id": task_id})

    def sub_task_failed(self, sub_index: int, error: str = "") -> TraceEvent:
        return self._emit(EventType.SUB_TASK_FAILED,
                          payload={"sub_index": sub_index, "error": error})

    def sub_task_skipped(self, sub_index: int, reason: str = "") -> TraceEvent:
        return self._emit(EventType.SUB_TASK_SKIPPED,
                          payload={"sub_index": sub_index, "reason": reason})

    def group_completed(self, status: str = "", count: int = 0) -> TraceEvent:
        return self._emit(EventType.GROUP_COMPLETED,
                          payload={"status": status, "completed_count": count})

    # ── parallel execution events ──────────────────────────────────

    def group_parallel_started(self, batch_count: int = 0) -> TraceEvent:
        return self._emit(EventType.GROUP_PARALLEL_STARTED,
                          payload={"batch_count": batch_count})

    def sub_task_batch_started(self, batch_id: int, indices: list[int]) -> TraceEvent:
        return self._emit(EventType.SUB_TASK_BATCH_STARTED,
                          payload={"batch_id": batch_id, "sub_indices": indices})

    def group_parallel_completed(self) -> TraceEvent:
        return self._emit(EventType.GROUP_PARALLEL_COMPLETED)

    # ── internal ─────────────────────────────────────────────────

    def _emit_raw(
        self,
        tool_name: str,
        event_type_str: str,
        *,
        step_id: str = "",
        capability: str = "",
        payload: dict | None = None,
        invocation_id: str = "",
        tool_call_id: str = "",
        operation_id: str = "",
        trace_context: TraceContext | None = None,
    ) -> TraceEvent:
        """Emit by string event type (used by ToolRuntime)."""
        try:
            et = EventType(event_type_str)
        except ValueError:
            et = EventType.TOOL_INVOKED
        context = self._event_context(
            invocation_id=invocation_id,
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            trace_context=trace_context,
        )
        return self._c.emit_event(
            trace_id=self.trace_id,
            event_type=et,
            execution_id=self.execution_id,
            step_id=step_id,
            tool_name=tool_name,
            capability=capability,
            payload=payload or {},
            trace_context=context,
        )

    def _emit(
        self,
        event_type: EventType,
        *,
        step_id: str = "",
        tool_name: str = "",
        capability: str = "",
        artifact_type: str = "",
        payload: dict | None = None,
        invocation_id: str = "",
        tool_call_id: str = "",
        operation_id: str = "",
        trace_context: TraceContext | None = None,
    ) -> TraceEvent:
        context = self._event_context(
            invocation_id=invocation_id,
            tool_call_id=tool_call_id,
            operation_id=operation_id,
            trace_context=trace_context,
        )
        return self._c.emit_event(
            trace_id=self.trace_id,
            event_type=event_type,
            execution_id=self.execution_id,
            step_id=step_id,
            tool_name=tool_name,
            capability=capability,
            artifact_type=artifact_type,
            payload=payload or {},
            trace_context=context,
        )

    def _event_context(
        self,
        *,
        invocation_id: str = "",
        tool_call_id: str = "",
        operation_id: str = "",
        trace_context: TraceContext | None = None,
    ) -> TraceContext:
        base = trace_context or self.context
        if invocation_id:
            return base.for_invocation(
                invocation_id,
                tool_call_id=tool_call_id or None,
                operation_id=operation_id or None,
            )
        return base.with_updates(
            tool_call_id=tool_call_id or None,
            operation_id=operation_id or None,
        )
