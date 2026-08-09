"""Phase 6.1 tests for GroupExecutor."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.group_executor import GroupExecutor
from greenbook_assistant_core.task.decomposer import (
    SubTaskContext,
    TaskDependency,
    TaskGroup,
)
from greenbook_assistant_core.task.models import TaskIntent


def _mock_ras(responses: list[RuntimeResult]) -> Any:
    """Build a mock RuntimeAgentService that returns canned results."""
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
    requirements: list | None = None,
    dep_index: int | None = None,
) -> SubTaskContext:
    intent = TaskIntent(
        relation=relation,
        goal_category=goal_category,
        goal=msg,
        requirements=requirements or [{"type": "CREATE"}],
    )
    return SubTaskContext(
        sub_index=index,
        user_message=msg,
        task_intent=intent,
        depends_on_task_index=dep_index,
    )


def _shared_ctx() -> RuntimeContext:
    return RuntimeContext(
        conversation_id="c1", run_id="r1", trace_id="t1",
        user_id="u1", tenant_id="t1",
        user_message="test",
        mcp=AsyncMock(), llm=AsyncMock(), model="test-model",
    )


# ── Case 1: Two independent CREATE → two independent results ────

@pytest.mark.asyncio
async def test_two_independent_creates() -> None:
    ras = _mock_ras([
        RuntimeResult(success=True, status="COMPLETED", task_id="task-a",
                      content="Created Java article", execution_path="runtime",
                      draft_id="draft-a"),
        RuntimeResult(success=True, status="COMPLETED", task_id="task-b",
                      content="Created Python article", execution_path="runtime",
                      draft_id="draft-b"),
    ])
    group = TaskGroup(sub_tasks=[
        _sub_task(0, "写一篇Java文章"),
        _sub_task(1, "写一篇Python文章"),
    ])

    ge = GroupExecutor(ras)
    result = await ge.execute(group, _shared_ctx())

    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.partial_results["sub_task_count"] == 2
    assert result.partial_results["completed_count"] == 2

    # SubTask task_ids assigned
    assert group.sub_tasks[0].task_id == "task-a"
    assert group.sub_tasks[1].task_id == "task-b"


# ── Case 2: Task0 fails, Task1 independent, Task2 depends on Task0 ──

@pytest.mark.asyncio
async def test_failure_with_dependency_chain() -> None:
    ras = _mock_ras([
        RuntimeResult(success=False, status="FAILED", task_id="task-a",
                      content="Java creation failed", execution_path="runtime"),
        RuntimeResult(success=True, status="COMPLETED", task_id="task-b",
                      content="Created Python article", execution_path="runtime",
                      draft_id="draft-b"),
        # Task 2 would be SKIPPED — no _execute_single call
    ])
    group = TaskGroup(sub_tasks=[
        _sub_task(0, "写一篇Java文章"),
        _sub_task(1, "写一篇Python文章"),
        _sub_task(2, "把第一篇文章发布时间改成晚上9点", dep_index=0,
                  goal_category="MANAGE_SCHEDULE", relation="MODIFY_TASK",
                  requirements=[{"type": "UPDATE"}]),
    ])

    ge = GroupExecutor(ras)
    result = await ge.execute(group, _shared_ctx())

    assert result.success is False
    assert result.status == "PARTIAL"
    assert result.partial_results["completed_count"] == 1  # Only Task1
    assert result.partial_results["failed_count"] == 2     # Task0 failed + Task2 skipped


# ── Case 3: Single task → still goes through _execute_single ─────

@pytest.mark.asyncio
async def test_single_task_still_calls_execute_single() -> None:
    call_count = 0

    class _RAS:
        async def _execute_single(self, ctx):
            nonlocal call_count
            call_count += 1
            return RuntimeResult(success=True, status="COMPLETED",
                                 content="done", execution_path="runtime")

    # Single SubTask simulates what execute() would do
    group = TaskGroup(sub_tasks=[_sub_task(0, "写一篇Spring教程")])
    ge = GroupExecutor(_RAS())
    result = await ge.execute(group, _shared_ctx())
    assert call_count == 1
    assert result.success is True


# ── Case 4: Cross-task dependency resource passing ──────────────

@pytest.mark.asyncio
async def test_cross_task_dependency_resources() -> None:
    """Task2 depends on Task0 → gets Task0's draft_id and schedule_id."""
    ras = _mock_ras([
        RuntimeResult(success=True, status="COMPLETED", task_id="task-a",
                      content="Created article", execution_path="runtime",
                      draft_id="draft-a", schedule_id="sched-a"),
        RuntimeResult(success=True, status="COMPLETED", task_id="task-c",
                      content="Modified schedule", execution_path="runtime"),
    ])
    sub0 = _sub_task(0, "写一篇Spring Boot文章")
    sub2 = _sub_task(2, "把第一篇文章发布时间改成晚上9点",
                     dep_index=0, goal_category="MANAGE_SCHEDULE",
                     relation="MODIFY_TASK", requirements=[{"type": "UPDATE"}])
    group = TaskGroup(sub_tasks=[sub0, sub2],
                      dependencies=[
                          TaskDependency(dependent_task_index=2, source_task_index=0,
                                         hint="第一篇文章"),
                      ])

    ge = GroupExecutor(ras)
    await ge.execute(group, _shared_ctx())

    # Task2 should have received the dependency resources
    assert sub2.dependency_resources.get("draft_id") == "draft-a"
    assert sub2.dependency_resources.get("schedule_id") == "sched-a"
    # And the sub_ctx should have task_id = "task-a"
    assert sub2.task_id == "task-c"  # from _execute_single result


# ── Case 5: Group result aggregation ────────────────────────────

@pytest.mark.asyncio
async def test_group_result_aggregation() -> None:
    ras = _mock_ras([
        RuntimeResult(success=True, status="COMPLETED", task_id="t1",
                      content="A done", execution_path="runtime",
                      tool_rounds=2, artifact_ids=["a1"]),
        RuntimeResult(success=True, status="COMPLETED", task_id="t2",
                      content="B done", execution_path="runtime",
                      tool_rounds=1, artifact_ids=["a2"]),
    ])
    group = TaskGroup(sub_tasks=[
        _sub_task(0, "Task A"),
        _sub_task(1, "Task B"),
    ])

    ge = GroupExecutor(ras)
    result = await ge.execute(group, _shared_ctx())

    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.tool_rounds == 3  # 2 + 1
    assert len(result.artifact_ids) == 2
    assert "[OK]" in result.content
    assert result.partial_results["group"] is True
