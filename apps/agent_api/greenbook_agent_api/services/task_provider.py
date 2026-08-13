"""Scoped Task persistence boundary for the canonical TaskManager.

This module is an API/storage adapter only.  It does not understand user
messages, Goals, plans, or execution control semantics.  Lifecycle
mutations belong to ``TaskManager``; terminal execution projections are the
one read-model write retained here for the completion publisher.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any

from greenbook_agent_core.db.connection import session_ctx
from greenbook_agent_core.task.models import (
    ArtifactRef,
    Task,
    TaskExecutionRef,
    TaskResourceRef,
    TaskStatus,
)
from greenbook_agent_core.task.registry import TaskRegistry
from greenbook_agent_core.task.repository import (
    TaskRepositoryError,
    TaskVersionConflict,
)


class TaskProviderError(ValueError):
    """Stable API-boundary error for Task scope and storage operations."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = tuple(str(value) for value in candidates)


@dataclass(frozen=True, slots=True)
class TaskScope:
    user_id: str
    tenant_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        for name in ("user_id", "tenant_id", "conversation_id"):
            if not str(getattr(self, name, "")).strip():
                raise TaskProviderError(
                    "TASK_SCOPE_INVALID",
                    f"Task scope requires a non-empty {name}.",
                )


class TaskProvider:
    """Scope-aware storage facade used by API projections and authorization."""

    def __init__(
        self,
        *,
        session_context_factory: Callable[[], AbstractAsyncContextManager[Any]]
        | None = None,
        registry_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._session_context_factory = session_context_factory or session_ctx
        self._registry_factory = registry_factory or TaskRegistry

    def canonical_repository(self) -> TaskProviderRepository:
        return TaskProviderRepository(self)

    async def ensure_storage(self) -> None:
        try:
            async with self._registry_context() as registry:
                ensure_tables = getattr(registry, "ensure_tables", None)
                if callable(ensure_tables):
                    result = ensure_tables()
                    if isawaitable(result):
                        await result
        except TaskProviderError:
            raise
        except Exception as exc:
            raise TaskProviderError(
                "TASK_STORE_INIT_FAILED",
                "The Task store could not be initialized.",
            ) from exc

    async def get_task(self, scope: TaskScope, task_id: str) -> Task | None:
        scope = self._coerce_scope(scope)
        if not str(task_id or "").strip():
            return None
        try:
            async with self._registry_context() as registry:
                task = registry.get_task(task_id)
                task = await task if isawaitable(task) else task
        except Exception as exc:
            raise TaskProviderError(
                "TASK_PROVIDER_UNAVAILABLE",
                "The Task store could not be queried.",
            ) from exc
        return task if task is not None and self._in_scope(task, scope) else None

    async def authorize_task(
        self,
        *,
        task_id: str,
        user_id: str,
        tenant_id: str,
    ) -> bool:
        if not all(str(value or "").strip() for value in (task_id, user_id, tenant_id)):
            return False
        try:
            async with self._registry_context() as registry:
                task = registry.get_task(task_id)
                task = await task if isawaitable(task) else task
        except Exception:
            return False
        return bool(
            task is not None
            and task.user_id == user_id
            and task.tenant_id == tenant_id
        )

    async def list_tasks(
        self,
        scope: TaskScope,
        *,
        statuses: Sequence[TaskStatus] | None = None,
    ) -> list[Task]:
        scope = self._coerce_scope(scope)
        try:
            async with self._registry_context() as registry:
                result = registry.list_tasks(scope.conversation_id)
                tasks = await result if isawaitable(result) else result
        except Exception as exc:
            raise TaskProviderError(
                "TASK_PROVIDER_UNAVAILABLE",
                "The Task store could not be queried.",
            ) from exc
        allowed = set(statuses or ())
        return [
            task
            for task in tasks
            if self._in_scope(task, scope)
            and (not allowed or task.status in allowed)
        ]

    async def persist_projection(self, scope: TaskScope, task: Task) -> Task | None:
        """Persist a read-model snapshot without changing lifecycle state."""

        scope = self._coerce_scope(scope)
        if not self._in_scope(task, scope):
            raise TaskProviderError("TASK_SCOPE_MISMATCH", "Task is outside scope.")
        try:
            async with self._registry_context() as registry:
                result = registry.update_task(
                    task.task_id,
                    goals=[item.model_dump(mode="json") for item in task.goals],
                    revisions=[item.model_dump(mode="json") for item in task.revisions],
                    execution_refs=[
                        item.model_dump(mode="json") for item in task.execution_refs
                    ],
                    resource_index=[
                        item.model_dump(mode="json") for item in task.resource_index
                    ],
                    last_action=task.last_action,
                    action_history=task.action_history,
                )
                return await result if isawaitable(result) else result
        except Exception as exc:
            raise TaskProviderError(
                "TASK_PROJECTION_PERSIST_FAILED",
                "The Task projection could not be written.",
            ) from exc

    async def persist_completion_projection(
        self,
        scope: TaskScope,
        *,
        task_id: str,
        execution_id: str,
        status: str,
        artifacts: Sequence[dict[str, Any]],
        error: str | None = None,
    ) -> Task | None:
        """Project terminal Execution facts into the Task read model."""

        scope = self._coerce_scope(scope)
        task = await self.get_task(scope, task_id)
        if task is None:
            return None
        normalized_status = str(status).upper()
        terminal_status = {
            "COMPLETED": TaskStatus.COMPLETED,
            "FAILED": TaskStatus.FAILED,
            "CANCELLED": TaskStatus.CANCELLED,
        }.get(normalized_status, task.status)
        now = datetime.now(UTC)

        artifact_refs = [item.model_copy(deep=True) for item in task.artifacts]
        resource_index = [item.model_copy(deep=True) for item in task.resource_index]
        for raw in artifacts:
            artifact_type = str(raw.get("type") or raw.get("artifact_type") or "")
            resource_kind = str(
                raw.get("resource_type") or _resource_type_from_artifact(artifact_type) or ""
            )
            artifact_id = str(raw.get("artifact_id") or "")
            if artifact_id:
                ref = ArtifactRef(
                    artifact_id=artifact_id,
                    task_id=task_id,
                    step_id=str(raw.get("step_id") or ""),
                    artifact_type=artifact_type,
                    resource_id=_optional_string(raw.get("resource_id")),
                    resource_kind=resource_kind or None,
                    summary=_optional_string(raw.get("summary")),
                )
                artifact_refs = [
                    item for item in artifact_refs if item.artifact_id != artifact_id
                ]
                artifact_refs.append(ref)
            resource_id = _optional_string(raw.get("resource_id"))
            if resource_id:
                resource = TaskResourceRef(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    title=_optional_string(raw.get("title")),
                    status=_optional_string(raw.get("status")),
                    scheduled_at=_optional_string(raw.get("run_at")),
                )
                resource_index = [
                    item for item in resource_index if item.resource_id != resource_id
                ]
                resource_index.append(resource)

        execution_refs = list(task.execution_refs)
        existing = next(
            (item for item in execution_refs if item.execution_id == execution_id),
            None,
        )
        if existing is None:
            execution_refs.append(TaskExecutionRef(
                execution_id=execution_id,
                task_id=task_id,
                status=normalized_status,
            ))
        else:
            existing.status = normalized_status
            existing.updated_at = now.isoformat()

        # A terminal Execution represents the compiled GoalTree as a whole.
        # Keep the Task's goal projection aligned with that durable runtime
        # fact; otherwise the API can report a completed Task while every
        # child Goal remains visually PENDING.
        is_current_execution = not task.active_execution_id or (
            task.active_execution_id == execution_id
        )
        goals = list(task.goals)
        if is_current_execution and normalized_status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }:
            for goal in goals:
                goal.status = normalized_status
                goal.execution_id = execution_id
                goal.updated_at = now.isoformat()
        projection_status = terminal_status if is_current_execution else task.status
        projection_completed_at = (
            now
            if is_current_execution
            and terminal_status
            in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            else task.completed_at
        )
        projection_goals = goals if is_current_execution else list(task.goals)
        projection_error = error if is_current_execution else task.last_error

        try:
            async with self._registry_context() as registry:
                result = registry.update_task(
                    task_id,
                    status=projection_status,
                    goals=[item.model_dump(mode="json") for item in projection_goals],
                    artifacts=[item.model_dump(mode="json") for item in artifact_refs],
                    execution_refs=[item.model_dump(mode="json") for item in execution_refs],
                    resource_index=[item.model_dump(mode="json") for item in resource_index],
                    last_error=projection_error,
                    completed_at=projection_completed_at,
                )
                return await result if isawaitable(result) else result
        except Exception as exc:
            raise TaskProviderError(
                "TASK_COMPLETION_PROJECTION_PERSIST_FAILED",
                "The terminal Execution projection could not be written.",
            ) from exc

    def _registry_context(self) -> AbstractAsyncContextManager[Any]:
        return self._registry_context_impl()

    @asynccontextmanager
    async def _registry_context_impl(self):
        async with self._session_context_factory() as session:
            yield self._registry_factory(session)

    @staticmethod
    def _coerce_scope(scope: TaskScope) -> TaskScope:
        if not isinstance(scope, TaskScope):
            raise TaskProviderError(
                "TASK_SCOPE_INVALID",
                "Task operations require a TaskScope.",
            )
        return scope

    @staticmethod
    def _in_scope(task: Task, scope: TaskScope) -> bool:
        return (
            task.conversation_id == scope.conversation_id
            and task.user_id == scope.user_id
            and task.tenant_id == scope.tenant_id
        )


class TaskProviderRepository:
    """Concrete adapter implementing the canonical TaskRepository protocol."""

    def __init__(self, provider: TaskProvider) -> None:
        self._provider = provider

    async def ensure_storage(self) -> None:
        await self._provider.ensure_storage()

    async def create(self, task: Task) -> Task:
        async with self._provider._registry_context() as registry:
            insert = getattr(registry, "insert_task", None)
            if not callable(insert):
                raise TaskRepositoryError("Task registry lacks insert_task().")
            result = insert(task)
            return await result if isawaitable(result) else result

    async def get(self, task_id: str) -> Task | None:
        async with self._provider._registry_context() as registry:
            result = registry.get_task(task_id)
            return await result if isawaitable(result) else result

    async def list(
        self,
        conversation_id: str,
        *,
        statuses: Sequence[TaskStatus] | None = None,
    ) -> list[Task]:
        async with self._provider._registry_context() as registry:
            result = registry.list_tasks(conversation_id)
            tasks = await result if isawaitable(result) else result
        allowed = set(statuses or ())
        return [item for item in tasks if not allowed or item.status in allowed]

    async def update(
        self,
        task: Task,
        *,
        expected_version: int | None = None,
    ) -> Task:
        async with self._provider._registry_context() as registry:
            current = registry.get_task(task.task_id)
            current = await current if isawaitable(current) else current
            if current is None:
                raise TaskRepositoryError(f"Task '{task.task_id}' does not exist.")
            if expected_version is not None and current.version != expected_version:
                raise TaskVersionConflict(
                    f"Task '{task.task_id}' version {current.version} != {expected_version}."
                )
            result = registry.update_task(
                task.task_id,
                **_task_storage_fields(task),
            )
            updated = await result if isawaitable(result) else result
            if updated is None:
                raise TaskRepositoryError(f"Task '{task.task_id}' update was not confirmed.")
            return updated


def _task_storage_fields(task: Task) -> dict[str, Any]:
    return {
        "goal": task.goal,
        "goal_category": task.goal_category,
        "goal_summary": task.goal_summary,
        "status": task.status,
        "phase": task.phase,
        "priority": task.priority,
        "task_type": task.task_type,
        "execution_mode": task.execution_mode,
        "root_goal_id": task.root_goal_id,
        "goal_tree_version": task.goal_tree_version,
        "goal_tree_snapshot": task.goal_tree_snapshot,
        "plan_version": task.plan_version,
        "plan_history": [item.model_dump(mode="json") for item in task.plan_history],
        "active_execution_id": task.active_execution_id,
        "artifacts": [item.model_dump(mode="json") for item in task.artifacts],
        "depends_on": task.depends_on,
        "goals": [item.model_dump(mode="json") for item in task.goals],
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


def _resource_type_from_artifact(artifact_type: str) -> str | None:
    normalized = str(artifact_type).upper()
    if normalized in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
        return "DRAFT"
    if normalized in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
        return "SCHEDULE"
    if normalized in {"POST", "PUBLISHED_POST"}:
        return "POST"
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


__all__ = [
    "TaskProvider",
    "TaskProviderRepository",
    "TaskProviderError",
    "TaskScope",
]
