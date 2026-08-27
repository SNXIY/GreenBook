"""Focused tests for the bounded independent-objective scheduler."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from greenbook_agent_core.actionloop import ActionLoop
from greenbook_agent_core.actionloop.models import ActionLoopResult
from greenbook_agent_core.task.models import Objective, Task, TaskStatus
from greenbook_agent_core.task.manager import TaskManager
from greenbook_agent_core.task.repository import InMemoryTaskRepository
from apps.agent_api.greenbook_agent_api.services.action_loop_executor import _to_runtime_result


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    async def _record(self, task: Any, event: str, detail: Any) -> None:
        self.events.append((event, detail))

    async def _record_resource(self, task: Any, resource_id: str, resource_kind: str,
                               title: str = "", content: str = "",
                               objective_id: str = "") -> None:
        task.resource_index.append({
            "resource_id": resource_id,
            "resource_kind": resource_kind,
            "objective_id": objective_id,
        })


class _Boundary:
    def __init__(self) -> None:
        self.submitted = 0
        self.unknown = 0

    def record_operation_submitted(self, **kwargs: Any) -> None:
        self.submitted += 1

    def record_result_unknown(self) -> None:
        self.unknown += 1

    def record_read(self) -> None:
        return None


def _task(*objectives: Objective) -> Task:
    return Task(
        task_id="parallel-task",
        conversation_id="conversation",
        user_id="user",
        tenant_id="tenant",
        status=TaskStatus.RUNNING,
        objectives=list(objectives),
        resource_index=[],
        execution_refs=[],
    )


def _draft(objective_id: str, *, dependencies: list[str] | None = None) -> Objective:
    return Objective(
        objective_id=objective_id,
        task_id="parallel-task",
        description=f"Draft {objective_id}",
        intent="CREATE_DRAFT",
        required_capabilities=["GENERATE_CONTENT"],
        expected_resource_kind="DRAFT",
        constraints={"title": f"Title {objective_id}", "instruction": "Write it"},
        dependencies=list(dependencies or []),
    )


@pytest.mark.asyncio
async def test_independent_create_objectives_are_submitted_concurrently() -> None:
    task = _task(_draft("o1"), _draft("o2"))
    store = _Store()
    boundary = _Boundary()
    active = 0
    maximum = 0
    calls: list[str] = []

    async def submitter(**kwargs: Any) -> dict[str, Any]:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        objective_id = str(kwargs["objective_id"])
        calls.append(objective_id)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "ok": True,
            "status": "SUBMITTED",
            "execution_id": f"execution-{objective_id}",
        }

    loop = ActionLoop(write_submitter=submitter, max_parallel_objectives=2)
    result = ActionLoopResult(task_id=task.task_id)
    started = await loop._try_parallel_independent_creates(
        task,
        None,
        SimpleNamespace(run_id="run", trace_id="trace"),
        boundary,
        store,
        result,
    )

    assert started is True
    assert maximum == 2
    assert set(calls) == {"o1", "o2"}
    assert result.status == "WAITING_EXTERNAL"
    assert result.partial_results["execution_ids"] == ["execution-o1", "execution-o2"]
    assert {
        item["objective_id"] for item in result.partial_results["parallel_results"]
    } == {"o1", "o2"}
    assert boundary.submitted == 2
    assert boundary.unknown == 1


@pytest.mark.asyncio
async def test_dependency_shape_is_not_parallelized() -> None:
    task = _task(_draft("o1"), _draft("o2", dependencies=["o1"]))
    loop = ActionLoop(write_submitter=lambda **kwargs: None)
    result = ActionLoopResult(task_id=task.task_id)

    started = await loop._try_parallel_independent_creates(
        task,
        None,
        SimpleNamespace(run_id="run", trace_id="trace"),
        _Boundary(),
        _Store(),
        result,
    )

    assert started is False
    assert result.observations == []


@pytest.mark.asyncio
async def test_parallel_submission_isolates_sibling_failure() -> None:
    task = _task(_draft("ok"), _draft("bad"))
    store = _Store()
    boundary = _Boundary()

    async def submitter(**kwargs: Any) -> dict[str, Any]:
        if kwargs["objective_id"] == "bad":
            raise RuntimeError("synthetic provider failure")
        return {"ok": True, "status": "SUBMITTED", "execution_id": "execution-ok"}

    result = ActionLoopResult(task_id=task.task_id)
    started = await ActionLoop(write_submitter=submitter)._try_parallel_independent_creates(
        task,
        None,
        SimpleNamespace(run_id="run", trace_id="trace"),
        boundary,
        store,
        result,
    )

    assert started is True
    assert result.status == "WAITING_EXTERNAL"
    by_objective = {
        item["objective_id"]: item
        for item in result.partial_results["parallel_results"]
    }
    assert by_objective["ok"]["status"] == "SUBMITTED"
    assert by_objective["bad"]["outcome"] == "FAILED"
    assert result.error_code == ""


@pytest.mark.asyncio
async def test_task_execution_projection_merges_concurrent_objective_binds() -> None:
    repository = InMemoryTaskRepository()
    manager = TaskManager(repository)
    task = _task(_draft("o1"), _draft("o2"))
    await repository.create(task)

    await asyncio.gather(
        manager.bind_execution(task.task_id, "execution-o1", goal_id="o1"),
        manager.bind_execution(task.task_id, "execution-o2", goal_id="o2"),
    )

    stored = await repository.get(task.task_id)
    assert stored is not None
    assert {
        (ref.execution_id, ref.goal_id) for ref in stored.execution_refs
    } == {("execution-o1", "o1"), ("execution-o2", "o2")}


def test_runtime_projection_preserves_all_parallel_execution_ids() -> None:
    result = ActionLoopResult(
        execution_id="execution-o1",
        partial_results={"execution_ids": ["execution-o1", "execution-o2"]},
    )

    projected = _to_runtime_result(result)

    assert projected.partial_results["execution_ids"] == [
        "execution-o1", "execution-o2"
    ]
