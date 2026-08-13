"""Evaluation of one persisted execution and its observed trace."""

from __future__ import annotations

from datetime import datetime

from greenbook_agent_core.execution.events import EventType, ExecutionEvent
from greenbook_agent_core.execution.models import (
    ExecutionStatus,
    PlanExecution,
    StepExecution,
    StepStatus,
)
from greenbook_agent_core.observability.models import EventType as TraceEventType
from greenbook_agent_core.observability.models import Trace
from pydantic import BaseModel, Field


class ExecutionRecord(BaseModel):
    execution: PlanExecution | None = None
    execution_id: str = ""
    events: list[ExecutionEvent] = Field(default_factory=list)
    steps: list[StepExecution] = Field(default_factory=list)
    trace: Trace | None = None

    def resolved_execution_id(self) -> str:
        return self.execution.execution_id if self.execution else self.execution_id

    def resolved_steps(self) -> list[StepExecution]:
        return self.steps or (self.execution.steps if self.execution else [])


class ExecutionEvaluation(BaseModel):
    execution_id: str
    success: bool
    latency: float = 0.0
    step_count: int = 0
    retry_count: int = 0
    failure_count: int = 0
    human_intervention: bool = False
    tool_call_count: int = 0
    tool_failure_count: int = 0
    quality_score: float = Field(ge=0.0, le=1.0)


class ExecutionEvaluator:
    """Compute deterministic runtime quality signals from recorded data."""

    def evaluate(self, record: ExecutionRecord) -> ExecutionEvaluation:
        execution = record.execution
        steps = record.resolved_steps()
        events = record.events
        execution_id = record.resolved_execution_id()

        status = execution.status if execution else None
        success = status == ExecutionStatus.COMPLETED
        retry_count = sum(step.retry_count for step in steps)
        failed_steps = sum(
            1 for step in steps
            if step.status in (StepStatus.FAILED, StepStatus.FAILED_RETRYABLE)
        )
        failure_events = sum(
            1 for event in events if event.event_type == EventType.STEP_FAILED
        )
        failure_count = max(failed_steps, failure_events)
        human_intervention = any(
            event.event_type == EventType.APPROVAL_REQUIRED
            for event in events
        ) or status == ExecutionStatus.WAITING_HUMAN
        tool_call_count = self._tool_call_count(record)
        tool_failure_count = self._tool_failure_count(record)
        latency = self._latency(record)

        completed = sum(1 for step in steps if step.status == StepStatus.COMPLETED)
        completion_ratio = completed / len(steps) if steps else float(success)
        quality = (
            0.5 * float(success)
            + 0.2 * completion_ratio
            + 0.1 * float(retry_count == 0)
            + 0.1 * float(failure_count == 0)
            + 0.1 * float(not human_intervention)
        )
        return ExecutionEvaluation(
            execution_id=execution_id,
            success=success,
            latency=latency,
            step_count=len(steps),
            retry_count=retry_count,
            failure_count=failure_count,
            human_intervention=human_intervention,
            tool_call_count=tool_call_count,
            tool_failure_count=tool_failure_count,
            quality_score=round(min(1.0, max(0.0, quality)), 6),
        )

    @staticmethod
    def _tool_call_count(record: ExecutionRecord) -> int:
        if record.trace is not None:
            return sum(
                1 for event in record.trace.events
                if event.event_type == TraceEventType.TOOL_INVOKED
            )
        return sum(
            1 for event in record.events
            if event.payload.get("tool_call") or event.payload.get("tool_name")
        )

    @staticmethod
    def _tool_failure_count(record: ExecutionRecord) -> int:
        if record.trace is not None:
            return sum(
                1 for event in record.trace.events
                if event.event_type == TraceEventType.TOOL_FAILED
            )
        return sum(
            1 for event in record.events
            if event.event_type == EventType.STEP_FAILED
            and bool(event.payload.get("tool_name"))
        )

    @staticmethod
    def _latency(record: ExecutionRecord) -> float:
        execution = record.execution
        if execution is None:
            return 0.0
        end = execution.completed_at or execution.updated_at
        if not execution.created_at or not end:
            return 0.0
        try:
            start_dt = datetime.fromisoformat(execution.created_at)
            end_dt = datetime.fromisoformat(end)
            return max(0.0, (end_dt - start_dt).total_seconds() * 1000)
        except (TypeError, ValueError):
            return 0.0


__all__ = ["ExecutionRecord", "ExecutionEvaluation", "ExecutionEvaluator"]
