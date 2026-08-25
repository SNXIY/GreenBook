"""Canonical persistence contracts for durable Tasks.

TaskManager owns lifecycle rules; repositories own storage.  The in-memory
implementation is intentionally useful for unit tests and local composition,
while ``TaskRegistryRepository`` adapts the existing PostgreSQL registry
without making TaskManager aware of SQLAlchemy.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from copy import deepcopy
from typing import Any, Protocol, runtime_checkable

from .models import Task, TaskStatus


class TaskRepositoryError(RuntimeError):
    """Base error for Task persistence failures."""


class TaskVersionConflictError(TaskRepositoryError):
    """Raised when a stale Task projection attempts to overwrite a newer one."""


TaskVersionConflict = TaskVersionConflictError


@runtime_checkable
class TaskRepository(Protocol):
    """Only persistence operations exposed to TaskManager."""

    async def ensure_storage(self) -> None: ...

    async def create(self, task: Task) -> Task: ...

    async def get(self, task_id: str) -> Task | None: ...

    async def list(
        self,
        conversation_id: str,
        *,
        statuses: Sequence[TaskStatus] | None = None,
    ) -> list[Task]: ...

    async def update(self, task: Task, *, expected_version: int | None = None) -> Task: ...


class InMemoryTaskRepository:
    """Concurrency-safe repository used by tests and embedded runtimes."""

    def __init__(self) -> None:
        self._items: dict[str, Task] = {}
        self._lock = asyncio.Lock()

    async def ensure_storage(self) -> None:
        return None

    async def create(self, task: Task) -> Task:
        async with self._lock:
            if task.task_id in self._items:
                raise TaskRepositoryError(f"Task '{task.task_id}' already exists.")
            stored = deepcopy(task)
            self._items[stored.task_id] = stored
            return deepcopy(stored)

    async def get(self, task_id: str) -> Task | None:
        async with self._lock:
            task = self._items.get(task_id)
            return deepcopy(task) if task is not None else None

    async def list(
        self,
        conversation_id: str,
        *,
        statuses: Sequence[TaskStatus] | None = None,
    ) -> list[Task]:
        allowed = set(statuses or ())
        async with self._lock:
            values = [
                task for task in self._items.values()
                if task.conversation_id == conversation_id
                and (not allowed or task.status in allowed)
            ]
            values.sort(key=lambda task: (-task.priority, task.updated_at), reverse=False)
            return deepcopy(values)

    async def update(self, task: Task, *, expected_version: int | None = None) -> Task:
        async with self._lock:
            current = self._items.get(task.task_id)
            if current is None:
                raise TaskRepositoryError(f"Task '{task.task_id}' does not exist.")
            if expected_version is not None and current.version != expected_version:
                raise TaskVersionConflict(
                    f"Task '{task.task_id}' version {current.version} != {expected_version}."
                )
            updated = deepcopy(task)
            updated.version = current.version + 1
            self._items[updated.task_id] = updated
            return deepcopy(updated)


class TaskRegistryRepository:
    """Adapter over the existing SQL-backed ``TaskRegistry``.

    The registry remains the concrete PostgreSQL implementation.  This
    adapter is the only shape that TaskManager sees, so future storage changes
    do not leak into lifecycle code.
    """

    def __init__(self, registry: Any) -> None:
        self._registry = registry

    async def ensure_storage(self) -> None:
        ensure = getattr(self._registry, "ensure_tables", None)
        if callable(ensure):
            result = ensure()
            if asyncio.iscoroutine(result):
                await result

    async def create(self, task: Task) -> Task:
        insert = getattr(self._registry, "insert_task", None)
        if not callable(insert):
            raise TaskRepositoryError("TaskRegistry does not expose insert_task().")
        result = insert(task)
        return await result if asyncio.iscoroutine(result) else result

    async def get(self, task_id: str) -> Task | None:
        result = self._registry.get_task(task_id)
        return await result if asyncio.iscoroutine(result) else result

    async def list(
        self,
        conversation_id: str,
        *,
        statuses: Sequence[TaskStatus] | None = None,
    ) -> list[Task]:
        result = self._registry.list_tasks(conversation_id)
        values = await result if asyncio.iscoroutine(result) else result
        allowed = set(statuses or ())
        return [task for task in values if not allowed or task.status in allowed]

    async def update(self, task: Task, *, expected_version: int | None = None) -> Task:
        current = await self.get(task.task_id)
        if current is None:
            raise TaskRepositoryError(f"Task '{task.task_id}' does not exist.")
        if expected_version is not None and current.version != expected_version:
            raise TaskVersionConflict(
                f"Task '{task.task_id}' version {current.version} != {expected_version}."
            )
        fields = {
            "goal": task.goal,
            "goal_category": task.goal_category,
            "goal_summary": task.goal_summary,
            "status": task.status,
            "phase": task.phase,
            "priority": task.priority,
            "task_type": task.task_type,
            "execution_mode": task.execution_mode,
            "requires_confirmation": task.requires_confirmation,
            "confirmation_state": task.confirmation_state,
            "confirmation_version": task.confirmation_version,
            "confirmed_version": task.confirmed_version,
            "confirmation_snapshot_hash": task.confirmation_snapshot_hash,
            "confirmation_resume_run_id": task.confirmation_resume_run_id,
            "root_goal_id": task.root_goal_id,
            "goal_tree_version": task.goal_tree_version,
            "goal_tree_snapshot": task.goal_tree_snapshot,
            "plan_version": task.plan_version,
            "plan_history": [item.model_dump(mode="json") for item in task.plan_history],
            "active_execution_id": task.active_execution_id,
            "artifacts": [item.model_dump(mode="json") for item in task.artifacts],
            "depends_on": task.depends_on,
            "goals": [item.model_dump(mode="json") for item in task.goals],
            "objectives": [item.model_dump(mode="json") for item in task.objectives],
            "revisions": [item.model_dump(mode="json") for item in task.revisions],
            "execution_refs": [item.model_dump(mode="json") for item in task.execution_refs],
            "resource_index": [item.model_dump(mode="json") for item in task.resource_index],
            "last_action": task.last_action,
            "action_history": task.action_history,
            "last_error": task.last_error,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "completed_at": task.completed_at,
        }
        update = self._registry.update_task
        if expected_version is None:
            result = update(task.task_id, **fields)
        else:
            result = update(
                task.task_id,
                expected_version=expected_version,
                **fields,
            )
        updated = await result if asyncio.iscoroutine(result) else result
        if updated is None:
            raise TaskRepositoryError(f"Task '{task.task_id}' disappeared during update.")
        return updated


# Explicit name for composition roots that prefer storage terminology.
PostgresTaskRepository = TaskRegistryRepository


__all__ = [
    "InMemoryTaskRepository",
    "PostgresTaskRepository",
    "TaskRegistryRepository",
    "TaskRepository",
    "TaskRepositoryError",
    "TaskVersionConflict",
    "TaskVersionConflictError",
]
