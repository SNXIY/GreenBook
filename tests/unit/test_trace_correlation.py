"""Phase 13-A correlation tests across Runtime observability boundaries."""

from __future__ import annotations

import sqlalchemy as sa

from greenbook_assistant_core.execution.events import EventType, ExecutionEvent
from greenbook_assistant_core.execution.evidence import ExecutionEvidence
from greenbook_assistant_core.execution.persistent_stores import PostgresExecutionEventStore
from greenbook_assistant_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)
from greenbook_assistant_core.observability.collector import TraceCollector
from greenbook_assistant_core.observability.context import TraceContext
from greenbook_assistant_core.observability.trace import AgentTrace


def _context() -> TraceContext:
    return TraceContext(
        conversation_id="conversation-13a",
        run_id="run-13a",
        trace_id="trace-13a",
        task_id="task-13a",
        execution_id="execution-13a",
    )


def test_trace_context_reaches_invocation_and_evidence() -> None:
    context = _context().for_step("step-13a")
    invocation = ToolInvocationContext.build(
        execution_id=context.execution_id,
        step_id=context.step_id,
        capability="GENERATE_CONTENT",
        tool_name="content.create_draft",
        tool_args={"title": "Trace"},
        trace_context=context,
    )

    evidence = ExecutionEvidence.from_context(invocation)

    assert invocation.trace_context.trace_id == "trace-13a"
    assert invocation.trace_context.invocation_id == invocation.invocation_id
    assert evidence.execution_id == "execution-13a"
    assert evidence.step_id == "step-13a"
    assert evidence.invocation_id == invocation.invocation_id
    assert evidence.trace_id == "trace-13a"


def test_agent_trace_binds_pre_execution_events_to_execution() -> None:
    collector = TraceCollector()
    trace = AgentTrace(
        collector,
        trace_context=TraceContext(
            run_id="run-13a",
            trace_id="trace-13a",
            task_id="task-13a",
        ),
    )
    trace.task_created("create content", "CREATE_CONTENT")
    trace.bind_context(_context())
    event = trace.execution_started()

    assert event.trace_context is not None
    assert event.trace_context.conversation_id == "conversation-13a"
    assert event.trace_context.execution_id == "execution-13a"
    assert collector.timeline("trace-13a")[0].trace_context.execution_id == "execution-13a"


def test_execution_event_context_survives_postgres_event_store_round_trip() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    store = PostgresExecutionEventStore(engine)
    context = _context().for_step("step-13a")
    store.append(
        ExecutionEvent(
            execution_id="execution-13a",
            event_type=EventType.STEP_COMPLETED,
            step_id="step-13a",
            trace_context=context,
            payload={"tool_name": "content.create_draft"},
        )
    )

    restored = store.list_events("execution-13a")

    assert len(restored) == 1
    assert restored[0].trace_context == context
    assert restored[0].payload["trace_context"]["trace_id"] == "trace-13a"
