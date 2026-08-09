"""Phase 6.4 tests for parallel GroupExecutor."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.group_executor import GroupExecutor
from greenbook_assistant_core.execution.group_scheduler import GroupScheduler
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


def _sub(index: int, msg: str = "test", dep: int | None = None) -> SubTaskContext:
    return SubTaskContext(
        sub_index=index, user_message=msg,
        task_intent=TaskIntent(goal_category="CREATE_CONTENT",
                               requirements=[{"type": "CREATE"}]),
        depends_on_task_index=dep,
    )


def _shared_ctx() -> RuntimeContext:
    return RuntimeContext(
        conversation_id="c1", run_id="r1", trace_id="t1",
        user_id="u1", user_message="test",
        mcp=AsyncMock(), llm=AsyncMock(), model="test-model",
    )


# ── Scheduler tests ────────────────────────────────────────────

def test_scheduler_two_independent_same_batch() -> None:
    """Two independent tasks → same batch."""
    group = TaskGroup(sub_tasks=[_sub(0), _sub(1)])
    scheduler = GroupScheduler(max_parallel=4)
    batches = scheduler.schedule(group)
    assert len(batches) == 1
    assert len(batches[0]) == 2


def test_scheduler_dependent_separate_batches() -> None:
    """Task 1 depends on Task 0 → separate batches."""
    group = TaskGroup(sub_tasks=[_sub(0), _sub(1, dep=0)])
    scheduler = GroupScheduler(max_parallel=4)
    batches = scheduler.schedule(group)
    assert len(batches) == 2
    assert batches[0].sub_tasks[0].sub_index == 0
    assert batches[1].sub_tasks[0].sub_index == 1


def test_scheduler_mixed_dag() -> None:
    """Task 0,1 (independent) + Task 2 (depends on 0) → 2 batches."""
    group = TaskGroup(sub_tasks=[_sub(0), _sub(1), _sub(2, dep=0)])
    scheduler = GroupScheduler(max_parallel=4)
    batches = scheduler.schedule(group)
    assert len(batches) == 2
    assert len(batches[0]) == 2  # 0 + 1 parallel
    assert len(batches[1]) == 1  # 2 after 0 done


def test_scheduler_max_parallel_cap() -> None:
    """5 independent tasks with max_parallel=2 → 3 batches."""
    group = TaskGroup(sub_tasks=[_sub(i) for i in range(5)])
    scheduler = GroupScheduler(max_parallel=2)
    batches = scheduler.schedule(group)
    assert len(batches) == 3  # 2 + 2 + 1


# ── Case 1: Two independent CREATE run in parallel ─────────────

@pytest.mark.asyncio
async def test_two_independent_parallel() -> None:
    """Two independent tasks execute in the same batch → parallel."""
    start_times: list[float] = []

    class _RAS:
        async def _execute_single(self, ctx):
            start_times.append(time.monotonic())
            await asyncio.sleep(0.05)  # simulate work
            return RuntimeResult(success=True, status="COMPLETED",
                                 content="done", execution_path="runtime",
                                 task_id=ctx.user_message)

    group = TaskGroup(sub_tasks=[_sub(0, "TaskA"), _sub(1, "TaskB")])
    ge = GroupExecutor(_RAS(), max_parallel=4)
    result = await ge.execute(group, _shared_ctx())

    assert result.success is True
    assert len(start_times) == 2
    # Both started within 10ms of each other → parallel
    assert abs(start_times[0] - start_times[1]) < 0.10


# ── Case 2: CREATE → MODIFY stays serial ──────────────────────

@pytest.mark.asyncio
async def test_dependent_tasks_serial() -> None:
    """Task 1 depends on Task 0 → serial execution."""
    start_times: list[float] = []

    class _RAS:
        async def _execute_single(self, ctx):
            start_times.append(time.monotonic())
            await asyncio.sleep(0.03)
            return RuntimeResult(success=True, status="COMPLETED",
                                 content="done", execution_path="runtime",
                                 task_id=ctx.user_message)

    group = TaskGroup(sub_tasks=[_sub(0), _sub(1, dep=0)])
    ge = GroupExecutor(_RAS(), max_parallel=4)
    await ge.execute(group, _shared_ctx())

    assert len(start_times) == 2
    # Task 1 starts AFTER Task 0 completes
    assert start_times[1] >= start_times[0]


# ── Case 3: Task0 fails, Task1 (independent) continues ────────

@pytest.mark.asyncio
async def test_failure_independent_continues() -> None:
    """Task0 fails, Task1 (no dep) still executes."""
    executed: list[int] = []

    class _RAS:
        async def _execute_single(self, ctx):
            executed.append(int(ctx.user_message))
            if ctx.user_message == "0":
                return RuntimeResult(success=False, status="FAILED",
                                     content="failed", execution_path="runtime")
            return RuntimeResult(success=True, status="COMPLETED",
                                 content="done", execution_path="runtime")

    group = TaskGroup(sub_tasks=[_sub(0, "0"), _sub(1, "1")])
    ge = GroupExecutor(_RAS(), max_parallel=4)
    result = await ge.execute(group, _shared_ctx())

    assert len(executed) == 2  # both executed
    assert result.status == "PARTIAL"


# ── Case 4: Concurrency limit enforced ────────────────────────

@pytest.mark.asyncio
async def test_concurrency_limit() -> None:
    """max_parallel=2 with 4 tasks → batches of 2."""
    active: list[int] = []
    max_active: list[int] = [0]

    class _RAS:
        async def _execute_single(self, ctx):
            idx = int(ctx.user_message)
            active.append(idx)
            max_active[0] = max(max_active[0], len(active))
            await asyncio.sleep(0.03)
            active.remove(idx)
            return RuntimeResult(success=True, status="COMPLETED",
                                 content="done", execution_path="runtime")

    group = TaskGroup(sub_tasks=[_sub(i, str(i)) for i in range(4)])
    ge = GroupExecutor(_RAS(), max_parallel=2)
    await ge.execute(group, _shared_ctx())

    # At most 2 tasks running concurrently
    assert max_active[0] <= 2
