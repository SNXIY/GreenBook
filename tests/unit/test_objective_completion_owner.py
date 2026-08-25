"""Objective completion ownership (Phase 8.3) — T4 suite.

A WRITE initiated under Objective A persists PlanExecution.objective_id=A.  When
that Execution completes the production completion callback must bind the
produced Resource to A even if the turn has since switched to Objective B.  The
resource must never fall through to the current/active Objective.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from greenbook_agent_core.task.models import (
    ArtifactRef,
    Objective,
    Task,
    TaskExecutionRef,
    TaskGoal,
    TaskResourceRef,
    TaskStatus,
)
from greenbook_agent_core.task.provider import TaskProvider, TaskScope
from greenbook_agent_core.task.objective_reducer import is_objective_satisfied


class _MemoryTaskRegistry:
    """Registry that round-trips objectives/goals/artifacts like the real one."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}

    def ensure_tables(self) -> None:
        return None

    def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def update_task(self, task_id: str, **fields: Any) -> Task:
        task = self._tasks[task_id]
        update: dict[str, Any] = {}
        typed_fields = {
            "goals": TaskGoal,
            "objectives": Objective,
            "artifacts": ArtifactRef,
            "resource_index": TaskResourceRef,
            "execution_refs": TaskExecutionRef,
        }
        for key, value in fields.items():
            if key not in type(task).model_fields:
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


def _provider(registry: _MemoryTaskRegistry) -> TaskProvider:
    return TaskProvider(
        session_context_factory=_memory_session,
        registry_factory=lambda _session: registry,
    )


def _two_objective_task() -> Task:
    return Task(
        task_id="t1",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="write",
        status=TaskStatus.RUNNING,
        objectives=[
            Objective(task_id="t1", objective_id="A", description="objective A"),
            Objective(task_id="t1", objective_id="B", description="objective B"),
        ],
        goals=[],
        artifacts=[],
        resource_index=[],
        execution_refs=[],
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
        "artifact_id": f"art-{resource_id}",
        "type": "SCHEDULE",
        "artifact_type": "SCHEDULE",
        "resource_type": "SCHEDULE",
        "resource_id": resource_id,
        "status": "SCHEDULED",
    }


def _strict_two_objective_task() -> Task:
    task = _two_objective_task()
    task.objectives = [
        Objective(task_id="t1", objective_id="A", description="objective A",
                  required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]),
        Objective(task_id="t1", objective_id="B", description="objective B",
                  required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]),
    ]
    return task


def _objective_by_id(task: Task, objective_id: str) -> Objective:
    return next(o for o in task.objectives if o.objective_id == objective_id)


@pytest.mark.asyncio
async def test_t4f_resource_binds_to_initiating_objective_after_switch() -> None:
    """A WRITE -> current switches to B -> A completes -> Resource still A-owned."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    # Objective A initiated the WRITE; the completion callback carries
    # PlanExecution.objective_id="A" (the initiating Objective), NOT the
    # current Objective B.
    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-A")],
        objective_id="A",
    )
    assert task is not None
    assert "draft-A" in _objective_by_id(task, "A").related_resource_ids


@pytest.mark.asyncio
async def test_t4_resource_owned_by_a_not_b() -> None:
    """R in A.related_resource_ids AND R not in B.related_resource_ids."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-A")],
        objective_id="A",
    )
    assert task is not None
    assert "draft-A" in _objective_by_id(task, "A").related_resource_ids
    assert "draft-A" not in _objective_by_id(task, "B").related_resource_ids


@pytest.mark.asyncio
async def test_typed_resource_replay_cannot_move_owner_between_objectives() -> None:
    """A typed ResourceRef keeps its initiating Objective across a bad replay."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    await provider.persist_completion_projection(
        _scope(), task_id="t1", execution_id="e-a", status="COMPLETED",
        artifacts=[_draft_artifact("draft-shared")], objective_id="A",
    )
    task = await provider.persist_completion_projection(
        _scope(), task_id="t1", execution_id="e-b", status="COMPLETED",
        artifacts=[_draft_artifact("draft-shared")], objective_id="B",
    )
    assert task is not None
    assert "draft-shared" in _objective_by_id(task, "A").related_resource_ids
    assert "draft-shared" not in _objective_by_id(task, "B").related_resource_ids
    refs = [item for item in task.resource_index if item.resource_id == "draft-shared"]
    assert len(refs) == 1
    assert refs[0].objective_id == "A"


@pytest.mark.asyncio
async def test_t4_missing_objective_id_binds_nothing() -> None:
    """objective_id=None -> resource never binds to B (or any Objective)."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-orphan")],
        objective_id=None,
    )
    assert task is not None
    assert "draft-orphan" not in _objective_by_id(task, "A").related_resource_ids
    assert "draft-orphan" not in _objective_by_id(task, "B").related_resource_ids


@pytest.mark.asyncio
async def test_t4_execution_ref_keeps_initiating_objective_owner() -> None:
    """The terminal ref retains A ownership for the Objective reducer."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e-a",
        status="FAILED",
        artifacts=[],
        objective_id="A",
    )

    assert task is not None
    ref = next(item for item in task.execution_refs if item.execution_id == "e-a")
    assert ref.goal_id == "A"


@pytest.mark.asyncio
async def test_t4_reload_keeps_objective_ownership() -> None:
    """persist/reload Task -> A.related_resource_ids still contains R."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-A")],
        objective_id="A",
    )
    # Reload from the registry (fresh Task projection, like a new process).
    reloaded = registry.get_task("t1")
    assert reloaded is not None
    assert "draft-A" in _objective_by_id(reloaded, "A").related_resource_ids
    assert "draft-A" not in _objective_by_id(reloaded, "B").related_resource_ids


@pytest.mark.asyncio
async def test_t4_non_matching_objective_id_binds_nothing() -> None:
    """An objective_id that matches no Objective must not guess the owner."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _two_objective_task()
    provider = _provider(registry)

    task = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e1",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-ghost")],
        objective_id="DOES-NOT-EXIST",
    )
    assert task is not None
    assert "draft-ghost" not in _objective_by_id(task, "A").related_resource_ids
    assert "draft-ghost" not in _objective_by_id(task, "B").related_resource_ids


@pytest.mark.asyncio
async def test_strict_objectives_do_not_share_resources_or_satisfaction() -> None:
    """A's draft/schedule never satisfy B, including after a reload."""
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _strict_two_objective_task()
    provider = _provider(registry)
    for execution_id, artifact in (
        ("e-a-draft", _draft_artifact("draft-A")),
        ("e-a-schedule", _schedule_artifact("schedule-A")),
    ):
        task = await provider.persist_completion_projection(
            _scope(), task_id="t1", execution_id=execution_id,
            status="COMPLETED", artifacts=[artifact], objective_id="A",
        )
        assert task is not None
    objective_a = _objective_by_id(task, "A")
    objective_b = _objective_by_id(task, "B")
    assert {"draft-A", "schedule-A"} <= set(objective_a.related_resource_ids)
    assert not objective_b.related_resource_ids
    assert is_objective_satisfied(task, objective_a)
    assert not is_objective_satisfied(task, objective_b)
    assert task.status == TaskStatus.RUNNING
    reloaded = registry.get_task("t1")
    assert reloaded is not None
    refs = {item.resource_id: item.objective_id for item in reloaded.resource_index}
    assert refs["draft-A"] == "A"
    assert refs["schedule-A"] == "A"


@pytest.mark.asyncio
async def test_task_completes_only_after_each_strict_objective_owns_outputs() -> None:
    registry = _MemoryTaskRegistry()
    registry._tasks["t1"] = _strict_two_objective_task()
    provider = _provider(registry)
    for execution_id, objective_id, artifact in (
        ("e-a-draft", "A", _draft_artifact("draft-A")),
        ("e-a-schedule", "A", _schedule_artifact("schedule-A")),
        ("e-b-draft", "B", _draft_artifact("draft-B")),
        ("e-b-schedule", "B", _schedule_artifact("schedule-B")),
    ):
        task = await provider.persist_completion_projection(
            _scope(), task_id="t1", execution_id=execution_id,
            status="COMPLETED", artifacts=[artifact], objective_id=objective_id,
        )
        assert task is not None
        if objective_id == "A":
            assert task.status == TaskStatus.RUNNING
    assert task.status == TaskStatus.COMPLETED
    assert all(objective.status == "COMPLETED" for objective in task.objectives)


@pytest.mark.asyncio
async def test_completed_mutation_execution_binds_operation_before_reducer() -> None:
    """Queue completion closes a mutation Objective, not just its resource ref."""
    registry = _MemoryTaskRegistry()
    task = Task(
        task_id="t1",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="update title",
        status=TaskStatus.RUNNING,
        objectives=[
            Objective(
                task_id="t1",
                objective_id="create-1",
                description="existing draft",
                status="COMPLETED",
                required_capabilities=["GENERATE_CONTENT"],
                related_resource_ids=["draft-1"],
            ),
            Objective(
                task_id="t1",
                objective_id="mutation-1",
                description="update existing draft",
                required_capabilities=["MANAGE_DRAFT"],
                related_resource_ids=["draft-1"],
                constraints={
                    "semantic_action": "UPDATE_DRAFT",
                    "target_objective_id": "create-1",
                },
            ),
        ],
        goals=[],
        artifacts=[],
        resource_index=[
            TaskResourceRef(
                resource_id="draft-1",
                resource_kind="DRAFT",
                objective_id="create-1",
                status="DRAFT",
            ),
        ],
        execution_refs=[],
    )
    registry._tasks["t1"] = task
    provider = _provider(registry)

    projected = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e-mutation",
        status="COMPLETED",
        artifacts=[_draft_artifact("draft-1")],
        objective_id="mutation-1",
    )

    assert projected is not None
    mutation = _objective_by_id(projected, "mutation-1")
    assert mutation.status == "COMPLETED"
    assert mutation.related_operations == ["e-mutation"]
    assert projected.status == TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_completed_delete_post_without_artifact_closes_mutation_objective() -> None:
    """A verified delete has no returned artifact but must not be resubmitted."""
    registry = _MemoryTaskRegistry()
    task = Task(
        task_id="t1",
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        goal="delete post",
        status=TaskStatus.RUNNING,
        objectives=[
            Objective(
                task_id="t1",
                objective_id="delete-1",
                description="delete existing post",
                required_capabilities=["DELETE_POST"],
                related_resource_ids=["post-1"],
                constraints={"semantic_action": "DELETE_POST"},
            ),
        ],
        goals=[],
        artifacts=[],
        resource_index=[
            TaskResourceRef(
                resource_id="post-1",
                resource_kind="POST",
                objective_id="delete-1",
                status="PUBLISHED",
            ),
        ],
        execution_refs=[],
    )
    registry._tasks["t1"] = task
    provider = _provider(registry)

    projected = await provider.persist_completion_projection(
        _scope(),
        task_id="t1",
        execution_id="e-delete",
        status="COMPLETED",
        artifacts=[],
        objective_id="delete-1",
    )

    assert projected is not None
    mutation = _objective_by_id(projected, "delete-1")
    assert mutation.status == "COMPLETED"
    assert mutation.related_operations == ["e-delete"]
    assert projected.status == TaskStatus.COMPLETED
