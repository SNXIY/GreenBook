"""Goal-aware completion semantics tests (Phase 2.6).

A terminal Execution completes its Action, never the Goal or the Task. The
Goal completes only when its desired business state is satisfied by real
resources; the Task completes only when every executable Goal is satisfied.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.task.models import Task, TaskGoal, TaskStatus
from greenbook_agent_core.task.provider import TaskProvider, TaskScope

SCHEDULED = "SCHEDULED_PUBLISH"
DRAFT_ONLY = "DRAFT_ONLY"


class _MemoryTaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def ensure_tables(self) -> None:
        return None

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def update_task(self, task_id: str, **fields: Any) -> Task:
        from greenbook_agent_core.task.models import (
            ArtifactRef,
            TaskExecutionRef,
            TaskResourceRef,
        )

        task = self._tasks[task_id]
        update: dict[str, Any] = {}
        model_fields = type(task).model_fields
        typed_fields = {
            "goals": TaskGoal,
            "artifacts": ArtifactRef,
            "resource_index": TaskResourceRef,
            "execution_refs": TaskExecutionRef,
        }
        for key, value in fields.items():
            if key not in model_fields:
                continue
            model_type = typed_fields.get(key)
            if model_type is not None:
                update[key] = [model_type.model_validate(item) for item in value]
            else:
                update[key] = value
        updated = task.model_copy(update=update)
        self._tasks[task_id] = updated
        return updated


@asynccontextmanager
async def _memory_session():
    yield None


def _scope() -> TaskScope:
    return TaskScope(user_id="u1", tenant_id="t1", conversation_id="c1")


def _task(
    *,
    goal: Goal,
    goal_status: str = "PENDING",
    active_execution_id: str = "e1",
) -> Task:
    goal_tree = GoalTree(root=goal)
    return Task(
        task_id="t1",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal=goal.description,
        status=TaskStatus.RUNNING,
        goals=[
            TaskGoal(
                goal_id=goal.goal_id,
                task_id="t1",
                description=goal.description,
                status=goal_status,
            )
        ],
        goal_tree_snapshot=goal_tree.model_dump(mode="json"),
        active_execution_id=active_execution_id,
        artifacts=[],
        resource_index=[],
        execution_refs=[],
    )


def _provider(registry: _MemoryTaskRegistry) -> TaskProvider:
    return TaskProvider(
        session_context_factory=_memory_session,
        registry_factory=lambda _session: registry,
    )


def _draft_artifact(resource_id: str = "draft-1") -> dict[str, Any]:
    return {
        "artifact_id": "art-1",
        "type": "DRAFT",
        "artifact_type": "DRAFT",
        "resource_type": "DRAFT",
        "resource_id": resource_id,
        "status": "DRAFT",
    }


def _schedule_artifact(resource_id: str = "schedule-1") -> dict[str, Any]:
    return {
        "artifact_id": "art-2",
        "type": "SCHEDULE",
        "artifact_type": "SCHEDULE",
        "resource_type": "SCHEDULE",
        "resource_id": resource_id,
        "status": "SCHEDULED",
    }


@pytest.mark.asyncio
async def test_generate_completion_does_not_complete_scheduled_goal() -> None:
    # §29: SCHEDULED Goal + Draft exists + Schedule missing
    # Action COMPLETED, Goal IN_PROGRESS, Task NOT completed.
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g1",
        description="Java 学习文章",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        temporal_constraint={"run_at": "T"},
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    ))
    provider = _provider(registry)
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact()],
        goal_id="g1",
    )
    assert task is not None
    assert task.status != TaskStatus.COMPLETED
    assert task.goals[0].status == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_draft_only_goal_completes_with_draft() -> None:
    # §30: DRAFT_ONLY Goal + owned Draft -> Goal satisfied, Task completed.
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g1",
        description="Java 并发草稿",
        goal_type="CREATE",
        publication_intent=DRAFT_ONLY,
        required_capabilities=["GENERATE_CONTENT"],
    ))
    provider = _provider(registry)
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact()],
        goal_id="g1",
    )
    assert task is not None
    assert task.goals[0].status == "COMPLETED"
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_schedule_completes_scheduled_goal() -> None:
    # §31: SCHEDULED Goal + owned Draft + owned Schedule -> satisfied.
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g1",
        description="Java 学习文章",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        temporal_constraint={"run_at": "T"},
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    ))
    provider = _provider(registry)
    # First execution (draft) leaves the Goal in progress.
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact()],
        goal_id="g1",
    )
    assert task is not None and task.goals[0].status == "IN_PROGRESS"
    # The runtime binds the new execution as active (TaskManager.bind_execution).
    registry._tasks["t1"] = task.model_copy(update={"active_execution_id": "e2"})
    # Second execution (schedule) plus the persisted draft resource satisfies it.
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e2",
        status="COMPLETED",
        artifacts=[_schedule_artifact()],
        goal_id="g1",
    )
    assert task is not None
    assert task.goals[0].status == "COMPLETED"
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_generate_marks_goal_failed() -> None:
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g1",
        description="Java 学习文章",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    ))
    provider = _provider(registry)
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="FAILED",
        artifacts=[],
        error="CREATOR_ERROR",
        goal_id="g1",
    )
    assert task is not None
    assert task.goals[0].status == "FAILED"
    assert task.status == TaskStatus.FAILED


# ── per-Goal resource isolation (design goal 0813) ─────────────────────────


def _multi_goal_task(goal_status: str = "PENDING") -> Task:
    tree = GoalTree(root=Goal(
        goal_id="root",
        description="two goals",
        goal_type="CREATE",
        children=[
            Goal(
                goal_id="g-a",
                description="write A",
                goal_type="CREATE",
                publication_intent=DRAFT_ONLY,
                required_capabilities=["GENERATE_CONTENT"],
            ),
            Goal(
                goal_id="g-b",
                description="write B",
                goal_type="CREATE",
                publication_intent=DRAFT_ONLY,
                required_capabilities=["GENERATE_CONTENT"],
            ),
        ],
    ))
    return Task(
        task_id="t-multi",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        status=TaskStatus.RUNNING,
        goals=[
            TaskGoal(task_id="t-multi", goal_id="root", description="two goals"),
            TaskGoal(task_id="t-multi", goal_id="g-a", description="write A"),
            TaskGoal(task_id="t-multi", goal_id="g-b", description="write B"),
        ],
        goal_tree_snapshot=tree.model_dump(mode="json"),
        active_execution_id="exec-a",
        artifacts=[],
        resource_index=[],
        execution_refs=[],
    )


@pytest.mark.asyncio
async def test_sibling_goal_artifact_does_not_satisfy_this_goal() -> None:
    """Goal B's draft must never complete Goal A (per-Goal ownership)."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t-multi"] = _multi_goal_task()
    provider = _provider(registry)

    # Execution A completes with A's own draft.
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t-multi",
        execution_id="exec-a",
        status="COMPLETED",
        artifacts=[{
            "artifact_id": "art-a",
            "type": "DRAFT",
            "resource_type": "DRAFT",
            "resource_id": "draft-a",
        }],
        goal_id="g-a",
    )
    assert task is not None
    statuses = {goal.goal_id: goal.status for goal in task.goals}
    assert statuses["g-a"] == "COMPLETED"
    # g-b has no execution yet: it must not be completed by g-a's draft.
    assert statuses["g-b"] in {"PENDING", "IN_PROGRESS"}


@pytest.mark.asyncio
async def test_goal_satisfied_by_own_persisted_resource_across_executions() -> None:
    """A Scheduled Goal is satisfied by its own persisted Draft + Schedule,
    even when they arrive in separate executions."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g1",
        description="Java 学习文章",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        temporal_constraint={"run_at": "T"},
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    ))
    provider = _provider(registry)
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-own")],
        goal_id="g1",
    )
    assert task is not None and task.goals[0].status == "IN_PROGRESS"
    registry._tasks["t1"] = task.model_copy(update={"active_execution_id": "e2"})
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e2",
        status="COMPLETED",
        artifacts=[_schedule_artifact("schedule-own")],
        goal_id="g1",
    )
    assert task is not None
    assert task.goals[0].status == "COMPLETED"
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_whole_plan_execution_keeps_legacy_projection() -> None:
    # A whole-plan Execution (no goal_id) represents the compiled GoalTree as
    # a whole: terminal Execution completes every Goal and the Task.
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g1",
        description="Java 学习文章",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
    ))
    provider = _provider(registry)
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact(), _schedule_artifact()],
    )
    assert task is not None
    assert task.goals[0].status == "COMPLETED"
    assert task.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_scheduled_goal_completes_with_schedule_and_persisted_draft() -> None:
    """Real-chain regression: a Scheduled-Publish Goal is satisfied by the
    Schedule it created plus the Draft it schedules.  The Draft is owned by
    the producing Goal (GENERATE_CONTENT), so in incremental mode it lives in
    the Task's persisted resource index, not in this Execution's own
    artifacts.  Without that fallback the schedule Goal stays IN_PROGRESS
    forever and the Task never reaches COMPLETED even after the schedule was
    durably created (observed: schedule SCHEDULED but task_status RUNNING)."""
    from greenbook_agent_core.task.models import TaskResourceRef

    registry = _MemoryTaskRegistry()
    task = _task(goal=Goal(
        goal_id="g5",
        description="五分钟后发布",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        temporal_constraint={"run_at": "T"},
        required_capabilities=["SCHEDULE_PUBLISH"],
    ))
    task.resource_index = [
        TaskResourceRef(
            resource_id="draft-346612960761876480",
            resource_kind="DRAFT",
            title="草稿",
            status="DRAFT",
        )
    ]
    registry._tasks["t1"] = task
    provider = _provider(registry)
    projected = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e5",
        status="COMPLETED",
        artifacts=[_schedule_artifact("schedule-346613060947021824")],
        goal_id="g5",
    )
    assert projected is not None
    assert projected.goals[0].status == "COMPLETED"
    assert projected.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_scheduled_goal_without_persisted_draft_stays_in_progress() -> None:
    """Without any durable Draft resource the schedule Goal must NOT complete:
    a Schedule alone never satisfies a Scheduled-Publish Goal."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _task(goal=Goal(
        goal_id="g5",
        description="五分钟后发布",
        goal_type="CREATE",
        publication_intent=SCHEDULED,
        temporal_constraint={"run_at": "T"},
        required_capabilities=["SCHEDULE_PUBLISH"],
    ))
    provider = _provider(registry)
    projected = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e5",
        status="COMPLETED",
        artifacts=[_schedule_artifact("schedule-1")],
        goal_id="g5",
    )
    assert projected is not None
    assert projected.goals[0].status == "IN_PROGRESS"
    assert projected.status != TaskStatus.COMPLETED
