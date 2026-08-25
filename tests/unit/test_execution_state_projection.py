"""Monotonic execution-state projection tests (S1-S5).

Invariant under test: for a single ``execution_id``, a terminal status
(COMPLETED / FAILED / CANCELLED) is a latch.  A late non-terminal
(QUEUED / RUNNING / PENDING) update must never regress it, and after an
execution goes terminal its ``active_execution_id`` pointer is cleared.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from greenbook_agent_core.actionloop import (
    ActionDecision,
    ActionDecisionType,
    ActionLoop,
)
from greenbook_agent_core.command.models import Command, CommandType
from greenbook_agent_core.goal.models import Goal
from greenbook_agent_core.task import TaskManager
from greenbook_agent_core.task.models import (
    ArtifactRef,
    Objective,
    ObjectiveStatus,
    Task,
    TaskExecutionRef,
    TaskGoal,
    TaskResourceRef,
    TaskStatus,
)
from greenbook_agent_core.task.provider import TaskProvider, TaskScope
from greenbook_agent_core.task.repository import (
    TaskRegistryRepository,
    TaskVersionConflict,
)


class _Registry:
    """Shared in-memory Task store exposing both the TaskManager repository
    protocol (via TaskRegistryRepository) and the TaskProvider registry API."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def ensure_tables(self) -> None:
        return None

    def insert_task(self, task: Task) -> Task:
        self._tasks[task.task_id] = task.model_copy(deep=True)
        return task.model_copy(deep=True)

    def get_task(self, task_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        return task.model_copy(deep=True) if task is not None else None

    def list_tasks(self, conversation_id: str, status: TaskStatus | None = None) -> list[Task]:
        values = [
            task
            for task in self._tasks.values()
            if task.conversation_id == conversation_id
            and (status is None or task.status == status)
        ]
        return [task.model_copy(deep=True) for task in values]

    def update_task(
        self,
        task_id: str,
        *,
        expected_version: int | None = None,
        **fields: Any,
    ) -> Task:
        current = self._tasks[task_id]
        if expected_version is not None and current.version != expected_version:
            raise TaskVersionConflict(
                f"Task '{task_id}' version {current.version} != {expected_version}."
            )
        typed_fields = {
            "goals": TaskGoal,
            "objectives": Objective,
            "artifacts": ArtifactRef,
            "resource_index": TaskResourceRef,
            "execution_refs": TaskExecutionRef,
        }
        update: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in type(current).model_fields:
                continue
            model_type = typed_fields.get(key)
            if model_type is not None and isinstance(value, (list, tuple)):
                update[key] = [model_type.model_validate(item) for item in value]
            else:
                update[key] = value
        updated = current.model_copy(update=update)
        updated.version = current.version + 1
        self._tasks[task_id] = updated
        return updated.model_copy(deep=True)


@asynccontextmanager
async def _memory_session():
    yield None


def _scope() -> TaskScope:
    return TaskScope(user_id="u1", tenant_id="t1", conversation_id="c1")


def _manager(registry: _Registry) -> TaskManager:
    return TaskManager(TaskRegistryRepository(registry))


def _provider(registry: _Registry) -> TaskProvider:
    return TaskProvider(
        session_context_factory=_memory_session,
        registry_factory=lambda _session: registry,
    )


async def _new_task(manager: TaskManager) -> Task:
    return await manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal=Goal(goal_id="g1", description="测试目标"),
    )


def _ref(task: Task, execution_id: str) -> TaskExecutionRef:
    return next(r for r in task.execution_refs if r.execution_id == execution_id)


# ── S1: QUEUED -> terminal CANCELLED -> ref CANCELLED + active cleared ─────


@pytest.mark.asyncio
async def test_s1_terminal_projection_clears_ref_and_active() -> None:
    registry = _Registry()
    manager = _manager(registry)
    provider = _provider(registry)
    task = await _new_task(manager)

    task = await manager.bind_execution(task.task_id, "e1", status="QUEUED")
    assert _ref(task, "e1").status == "QUEUED"
    assert task.active_execution_id == "e1"

    projected = await provider.persist_completion_projection(
        _scope(),
        task_id=task.task_id,
        execution_id="e1",
        status="CANCELLED",
        artifacts=[],
    )
    assert projected is not None
    assert _ref(projected, "e1").status == "CANCELLED"
    assert projected.active_execution_id is None


# ── S2: CANCELLED -> late QUEUED -> still CANCELLED ────────────────────────


@pytest.mark.asyncio
async def test_s2_terminal_ref_survives_late_queued_rebind() -> None:
    registry = _Registry()
    manager = _manager(registry)
    provider = _provider(registry)
    task = await _new_task(manager)

    task = await manager.bind_execution(task.task_id, "e1", status="QUEUED")
    task = await provider.persist_completion_projection(
        _scope(), task_id=task.task_id, execution_id="e1", status="CANCELLED", artifacts=[]
    )
    assert _ref(task, "e1").status == "CANCELLED"

    rebound = await manager.bind_execution(task.task_id, "e1", status="QUEUED")
    assert _ref(rebound, "e1").status == "CANCELLED"
    assert rebound.active_execution_id is None


# ── S3: COMPLETED -> late RUNNING -> still COMPLETED ───────────────────────


@pytest.mark.asyncio
async def test_s3_completed_ref_survives_late_running() -> None:
    registry = _Registry()
    manager = _manager(registry)
    provider = _provider(registry)
    task = await _new_task(manager)

    task = await manager.bind_execution(task.task_id, "e1", status="QUEUED")
    task = await provider.persist_completion_projection(
        _scope(), task_id=task.task_id, execution_id="e1", status="COMPLETED", artifacts=[]
    )
    assert _ref(task, "e1").status == "COMPLETED"
    assert task.active_execution_id is None

    rebound = await manager.bind_execution(task.task_id, "e1", status="RUNNING")
    assert _ref(rebound, "e1").status == "COMPLETED"
    assert rebound.active_execution_id is None


# ── S4: normal QUEUED -> RUNNING -> COMPLETED still works ──────────────────


@pytest.mark.asyncio
async def test_s4_normal_progression_still_works() -> None:
    registry = _Registry()
    manager = _manager(registry)
    provider = _provider(registry)
    task = await _new_task(manager)

    task = await manager.bind_execution(task.task_id, "e1", status="QUEUED")
    assert _ref(task, "e1").status == "QUEUED"

    task = await manager.bind_execution(task.task_id, "e1", status="RUNNING")
    assert _ref(task, "e1").status == "RUNNING"
    assert task.active_execution_id == "e1"

    task = await provider.persist_completion_projection(
        _scope(), task_id=task.task_id, execution_id="e1", status="COMPLETED", artifacts=[]
    )
    assert _ref(task, "e1").status == "COMPLETED"
    assert task.active_execution_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [TaskStatus.RUNNING, TaskStatus.FAILED])
async def test_superseded_objective_does_not_project_task_failure(initial_status: TaskStatus) -> None:
    registry = _Registry()
    provider = _provider(registry)
    old = Objective(
        task_id="task-supersede",
        objective_id="old-mutation",
        intent="UPDATE_SCHEDULE",
        status=ObjectiveStatus.FAILED,
        constraints={
            "mutation_status": "SUPERSEDED",
            "superseded_by": "new-mutation",
        },
    )
    new = Objective(
        task_id="task-supersede",
        objective_id="new-mutation",
        intent="UPDATE_SCHEDULE",
        required_capabilities=["MANAGE_SCHEDULE"],
        status=ObjectiveStatus.PENDING,
    )
    task = Task(
        task_id="task-supersede",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        status=initial_status,
        objectives=[old, new],
    )
    registry.insert_task(task)

    projected = await provider.persist_completion_projection(
        _scope(),
        task_id=task.task_id,
        execution_id="e-new",
        status="COMPLETED",
        artifacts=[],
        objective_id=new.objective_id,
    )

    assert projected is not None
    assert projected.status == TaskStatus.READY
    assert projected.objectives[0].status == ObjectiveStatus.SUPERSEDED
    assert not any(
        item.status == ObjectiveStatus.FAILED
        and item.constraints.get("mutation_status") != "SUPERSEDED"
        for item in projected.objectives
    )


# ── S5: ActionLoop does not WAITING_EXTERNAL on a terminal stale ref ───────


class _RecordingStore:
    def _record(self, task: Any, event: str, detail: Any) -> None:
        task.last_action = event

    def _record_resource(self, *args: Any, **kwargs: Any) -> None:
        return None


class _QueueDecisions:
    def __init__(self, decisions: list[ActionDecision]) -> None:
        self._queue = list(decisions)
        self.calls = 0

    async def __call__(self, context: Any) -> ActionDecision:
        self.calls += 1
        return self._queue.pop(0)


def _command() -> Command:
    return Command(type=CommandType.CREATE, goal="任务", raw_input="任务")


def _request() -> Any:
    return type("Req", (), {
        "run_id": "run1", "trace_id": "trace1", "conversation_id": "c1",
        "user_id": "u1", "tenant_id": "t1", "session": None, "auth": None,
        "mcp": None, "llm": None, "model": "", "timezone": "Asia/Shanghai",
        "activity_callback": None, "completion_callback": None,
    })()


@pytest.mark.asyncio
async def test_s5_terminal_stale_ref_does_not_block_loop() -> None:
    task = Task(
        task_id="t5",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="目标",
        status=TaskStatus.RUNNING,
        goals=[],
        execution_refs=[TaskExecutionRef(execution_id="e1", task_id="t5", status="CANCELLED")],
        active_execution_id="e1",
        resource_index=[],
        artifacts=[],
    )
    decisions = _QueueDecisions([ActionDecision(decision=ActionDecisionType.FINISH)])
    loop = ActionLoop(decision_maker=decisions, task_store=_RecordingStore(), max_iterations=4)
    result = await loop.run(task, _command(), request=_request())
    assert result.status != "WAITING_EXTERNAL"
    assert decisions.calls == 1, "the terminal ref must not short-circuit the loop"
