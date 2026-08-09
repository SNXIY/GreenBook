"""Phase 6.2.1 tests for Group trace events."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.group_executor import GroupExecutor
from greenbook_assistant_core.observability.collector import TraceCollector
from greenbook_assistant_core.observability.models import EventType
from greenbook_assistant_core.observability.trace import AgentTrace
from greenbook_assistant_core.task.decomposer import (
    SubTaskContext,
    TaskGroup,
)
from greenbook_assistant_core.task.models import TaskIntent


def _mock_ras(responses: list[RuntimeResult]) -> Any:
    class _RAS:
        async def _execute_single(self, ctx):
            if not responses:
                return RuntimeResult(success=True, status="COMPLETED",
                                     content="done", execution_path="runtime")
            return responses.pop(0)
    return _RAS()


def _sub_task(
    index: int, msg: str,
    goal_category: str = "CREATE_CONTENT",
    relation: str = "NEW_TASK",
    dep_index: int | None = None,
) -> SubTaskContext:
    return SubTaskContext(
        sub_index=index, user_message=msg,
        task_intent=TaskIntent(
            relation=relation, goal_category=goal_category, goal=msg,
            requirements=[{"type": "CREATE"}],
        ),
        depends_on_task_index=dep_index,
    )


def _shared_ctx() -> RuntimeContext:
    return RuntimeContext(
        conversation_id="c1", run_id="r1", trace_id="t1",
        user_id="u1", tenant_id="t1", user_message="test",
        mcp=AsyncMock(), llm=AsyncMock(), model="test-model",
    )


# ── Case 1: two independent tasks — complete trace ──────────────

@pytest.mark.asyncio
async def test_two_independent_tasks_full_trace() -> None:
    collector = TraceCollector()
    trace = AgentTrace(collector, trace_id="t-1")
    ras = _mock_ras([
        RuntimeResult(success=True, status="COMPLETED", task_id="task-a",
                      content="A done", execution_path="runtime"),
        RuntimeResult(success=True, status="COMPLETED", task_id="task-b",
                      content="B done", execution_path="runtime"),
    ])
    group = TaskGroup(sub_tasks=[
        _sub_task(0, "写Java文章"),
        _sub_task(1, "写Python文章"),
    ])

    ge = GroupExecutor(ras, trace=trace)
    await ge.execute(group, _shared_ctx())

    events = collector.timeline("t-1")
    event_types = [e.event_type for e in events]

    assert EventType.GROUP_CREATED in event_types
    assert event_types.count(EventType.SUB_TASK_STARTED) == 2
    assert event_types.count(EventType.SUB_TASK_COMPLETED) == 2
    assert EventType.GROUP_COMPLETED in event_types

    # Verify order: GROUP_CREATED → SUB_TASK_STARTED → SUB_TASK_COMPLETED → ...
    gc_idx = event_types.index(EventType.GROUP_CREATED)
    first_start = event_types.index(EventType.SUB_TASK_STARTED)
    last_done = max(i for i, t in enumerate(event_types)
                    if t == EventType.SUB_TASK_COMPLETED)
    gc_end = event_types.index(EventType.GROUP_COMPLETED)

    assert gc_idx < first_start
    assert last_done < gc_end


# ── Case 2: one fail, one success ───────────────────────────────

@pytest.mark.asyncio
async def test_one_fail_one_success_trace() -> None:
    collector = TraceCollector()
    trace = AgentTrace(collector, trace_id="t-2")
    ras = _mock_ras([
        RuntimeResult(success=False, status="FAILED", task_id="task-a",
                      content="A failed", execution_path="runtime",
                      error_code="ERROR", error_message="Creation failed"),
        RuntimeResult(success=True, status="COMPLETED", task_id="task-b",
                      content="B done", execution_path="runtime"),
    ])
    group = TaskGroup(sub_tasks=[
        _sub_task(0, "写Java文章"),
        _sub_task(1, "写Python文章"),
    ])

    ge = GroupExecutor(ras, trace=trace)
    await ge.execute(group, _shared_ctx())

    events = collector.timeline("t-2")
    event_types = [e.event_type for e in events]

    assert EventType.SUB_TASK_FAILED in event_types
    assert EventType.SUB_TASK_COMPLETED in event_types
    assert EventType.GROUP_COMPLETED in event_types

    # GROUP_COMPLETED should indicate PARTIAL
    gc_event = next(e for e in events if e.event_type == EventType.GROUP_COMPLETED)
    assert gc_event.payload["status"] == "PARTIAL"
    assert gc_event.payload["completed_count"] == 1


# ── Case 3: dependency task skipped trace ───────────────────────

@pytest.mark.asyncio
async def test_dependency_skip_trace() -> None:
    collector = TraceCollector()
    trace = AgentTrace(collector, trace_id="t-3")
    ras = _mock_ras([
        RuntimeResult(success=False, status="FAILED", task_id="task-a",
                      content="A failed", execution_path="runtime"),
        RuntimeResult(success=True, status="COMPLETED", task_id="task-b",
                      content="B done", execution_path="runtime"),
        # Task 2 would be SKIPPED
    ])
    group = TaskGroup(sub_tasks=[
        _sub_task(0, "写Java文章"),
        _sub_task(1, "写Python文章"),
        _sub_task(2, "修改第一篇文章时间", dep_index=0,
                  goal_category="MANAGE_SCHEDULE", relation="MODIFY_TASK"),
    ])

    ge = GroupExecutor(ras, trace=trace)
    await ge.execute(group, _shared_ctx())

    events = collector.timeline("t-3")
    event_types = [e.event_type for e in events]

    assert EventType.SUB_TASK_FAILED in event_types    # Task 0 failed
    assert EventType.SUB_TASK_COMPLETED in event_types  # Task 1 completed
    assert EventType.SUB_TASK_SKIPPED in event_types    # Task 2 skipped

    # Verify skip event has reason
    skip_events = [e for e in events if e.event_type == EventType.SUB_TASK_SKIPPED]
    assert len(skip_events) == 1
    assert skip_events[0].payload["sub_index"] == 2
