"""Request-scoped Task binding boundary for the Assistant Runtime.

This module deliberately stops at Task/TaskBinding.  Intent understanding,
TaskContext compilation, planning, execution, and execution cancellation stay
in their existing layers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from greenbook_assistant_core.db.connection import session_ctx
from greenbook_assistant_core.task.intent_compat import to_task_intent
from greenbook_assistant_core.task.intent_models import IntentSpec
from greenbook_assistant_core.task.models import (
    ResolvedTaskTarget,
    Task,
    TaskIntent,
    TaskStatus,
)
from greenbook_assistant_core.task.registry import TaskRegistry
from greenbook_assistant_core.task.resolver import TaskResolver


class TaskProviderError(ValueError):
    """Stable, API-boundary error for Task scope and target operations."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = tuple(str(candidate) for candidate in candidates)


@dataclass(frozen=True, slots=True)
class TaskScope:
    """Authenticated scope in which a Task may be read or changed."""

    user_id: str
    tenant_id: str
    conversation_id: str

    def __post_init__(self) -> None:
        for field_name in ("user_id", "tenant_id", "conversation_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise TaskProviderError(
                    "TASK_SCOPE_INVALID",
                    f"Task scope requires a non-empty {field_name}.",
                )


@dataclass(frozen=True, slots=True)
class TaskBinding:
    """A scoped Task plus the resolver evidence for its target binding."""

    task: Task
    target: ResolvedTaskTarget


class TaskProvider:
    """Create and resolve Tasks inside a request-scoped database session.

    ``session_context_factory`` and ``registry_factory`` are injectable so the
    boundary can be tested without a database.  Production defaults use the
    existing ``session_ctx`` and ``TaskRegistry`` implementations.
    """

    def __init__(
        self,
        *,
        session_context_factory: Callable[[], AbstractAsyncContextManager[Any]]
        | None = None,
        registry_factory: Callable[[Any], Any] | None = None,
        resolver: TaskResolver | None = None,
    ) -> None:
        self._session_context_factory = session_context_factory or session_ctx
        self._registry_factory = registry_factory or TaskRegistry
        self._resolver = resolver or TaskResolver()

    async def create_task(
        self,
        scope: TaskScope,
        intent_spec: IntentSpec | dict[str, Any],
    ) -> Task:
        """Persist a new READY Task from a validated IntentSpec.

        The semantic provider is the source of the validated Spec.  We still
        perform a schema gate here because this is an independent API boundary;
        no natural-language understanding or legacy fallback is performed.
        """

        scope = self._coerce_scope(scope)
        spec = self._coerce_intent_spec(intent_spec)
        if not spec.goal.strip() or not spec.actions:
            raise TaskProviderError(
                "INTENT_SPEC_INVALID",
                "A new Task requires a non-empty goal and at least one action.",
            )

        task_intent = to_task_intent(spec)
        task_id = str(uuid.uuid4())

        try:
            async with self._registry_context() as registry:
                task = await registry.create_task(
                    conversation_id=scope.conversation_id,
                    user_id=scope.user_id,
                    tenant_id=scope.tenant_id,
                    task_id=task_id,
                    goal=spec.goal,
                    goal_category=str(task_intent.goal_category),
                    goal_summary=spec.goal,
                    status=TaskStatus.READY,
                )
        except TaskProviderError:
            raise
        except Exception as exc:
            raise TaskProviderError(
                "TASK_CREATE_FAILED",
                "The new Task could not be persisted.",
            ) from exc

        if not self._in_scope(task, scope):
            raise TaskProviderError(
                "TASK_SCOPE_MISMATCH",
                "The persisted Task does not belong to the requested scope.",
            )
        if task.status != TaskStatus.READY:
            raise TaskProviderError(
                "TASK_STATE_CONFLICT",
                "A new Runtime Task must be persisted with READY status.",
            )
        return task

    async def get_task(
        self,
        scope: TaskScope,
        task_id: str,
    ) -> Task | None:
        """Return a Task only when it belongs to the authenticated scope."""

        scope = self._coerce_scope(scope)
        if not isinstance(task_id, str) or not task_id.strip():
            return None

        try:
            async with self._registry_context() as registry:
                task = await registry.get_task(task_id)
        except (TypeError, ValueError):
            # Invalid/untrusted IDs must not escape the scope boundary.
            return None
        except Exception as exc:
            raise TaskProviderError(
                "TASK_PROVIDER_UNAVAILABLE",
                "The Task store could not be queried.",
            ) from exc

        if task is None or not self._in_scope(task, scope):
            return None
        return task

    async def authorize_task(
        self,
        *,
        task_id: str,
        user_id: str,
        tenant_id: str,
    ) -> bool:
        """Check execution ownership without accepting a conversation hint.

        Execution resources carry the task id, but a request authorizer must
        not trust a caller-provided conversation id to establish ownership.
        This narrow lookup compares the persisted task's user and tenant
        scope only and deliberately returns ``False`` for malformed or
        unavailable records so the HTTP boundary can fail closed.
        """

        if not all(
            isinstance(value, str) and value.strip()
            for value in (task_id, user_id, tenant_id)
        ):
            return False

        try:
            async with self._registry_context() as registry:
                task = await registry.get_task(task_id)
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
        """List only Tasks owned by the supplied scope."""

        scope = self._coerce_scope(scope)
        try:
            async with self._registry_context() as registry:
                tasks = await registry.list_tasks(scope.conversation_id)
        except Exception as exc:
            raise TaskProviderError(
                "TASK_PROVIDER_UNAVAILABLE",
                "The Task store could not be queried.",
            ) from exc

        allowed_statuses = set(statuses or ())
        return [
            task
            for task in tasks
            if self._in_scope(task, scope)
            and (not allowed_statuses or task.status in allowed_statuses)
        ]

    async def resolve_task(
        self,
        scope: TaskScope,
        intent: TaskIntent | IntentSpec | dict[str, Any],
    ) -> TaskBinding:
        """Resolve a CONTINUE/MODIFY/CANCEL intent to a scoped TaskBinding."""

        scope = self._coerce_scope(scope)
        task_intent = self._coerce_task_intent(intent)
        relation = str(task_intent.relation)
        if relation not in {"CONTINUE_TASK", "MODIFY_TASK", "CANCEL_TASK"}:
            raise TaskProviderError(
                "TASK_TARGET_REQUIRED",
                "Only CONTINUE_TASK, MODIFY_TASK, and CANCEL_TASK need Task resolution.",
            )

        tasks = await self.list_tasks(scope)
        if not tasks:
            raise TaskProviderError(
                "TASK_NOT_FOUND",
                "No Task in the requested scope matches this operation.",
            )

        target = self._resolver.resolve(task_intent, tasks)
        if target is None:
            raise TaskProviderError(
                "TASK_NOT_FOUND",
                "No Task matches the requested target.",
            )

        if target.is_ambiguous or target.candidates:
            candidates = list(target.candidates)
            if target.task_id and target.task_id not in candidates:
                candidates.insert(0, target.task_id)
            raise TaskProviderError(
                "TASK_TARGET_AMBIGUOUS",
                "Multiple Tasks match the requested target.",
                candidates=candidates,
            )

        # A non-empty hint must have produced a meaningful label/artifact/id
        # match.  A purely temporal hint may use recency only when there is
        # exactly one scoped candidate; never silently guess among several.
        temporal_only = False
        if task_intent.target_task_hint:
            temporal_only = self._resolver._is_temporal_only(  # type: ignore[attr-defined]
                task_intent.target_task_hint,
            )
        if (
            task_intent.target_task_hint
            and target.match_level >= 4
            and not (temporal_only and len(tasks) == 1)
        ):
            raise TaskProviderError(
                "TASK_NOT_FOUND",
                "The referenced Task could not be matched.",
            )

        if not task_intent.target_task_id and not task_intent.target_task_hint:
            category_matches = [
                task
                for task in tasks
                if not task_intent.goal_category
                or task.goal_category == task_intent.goal_category
            ]
            if len(category_matches) > 1:
                raise TaskProviderError(
                    "TASK_TARGET_AMBIGUOUS",
                    "Multiple Tasks are eligible for this operation.",
                    candidates=[task.task_id for task in category_matches],
                )

        task = next((item for item in tasks if item.task_id == target.task_id), None)
        if task is None or not self._in_scope(task, scope):
            raise TaskProviderError(
                "TASK_NOT_FOUND",
                "The resolved Task is not available in the requested scope.",
            )
        if task.status == TaskStatus.CANCELLED:
            raise TaskProviderError(
                "TASK_NOT_ACTIVE",
                "The resolved Task has already been cancelled.",
            )
        return TaskBinding(task=task, target=target)

    async def cancel_task(
        self,
        scope: TaskScope,
        intent: TaskIntent | IntentSpec | dict[str, Any],
    ) -> Task:
        """Mark a business Task CANCELLED without touching Runtime execution."""

        scope = self._coerce_scope(scope)
        task_intent = self._coerce_task_intent(intent)
        if str(task_intent.relation) != "CANCEL_TASK":
            raise TaskProviderError(
                "TASK_TARGET_REQUIRED",
                "cancel_task requires a CANCEL_TASK intent.",
            )

        binding = await self.resolve_task(scope, task_intent)
        try:
            async with self._registry_context() as registry:
                task = await registry.update_task(
                    binding.task.task_id,
                    status=TaskStatus.CANCELLED,
                )
        except TaskProviderError:
            raise
        except Exception as exc:
            raise TaskProviderError(
                "TASK_CANCEL_FAILED",
                "The Task could not be cancelled.",
            ) from exc

        if task is None or not self._in_scope(task, scope):
            raise TaskProviderError(
                "TASK_CANCEL_FAILED",
                "The cancelled Task could not be confirmed in scope.",
            )
        if task.status != TaskStatus.CANCELLED:
            raise TaskProviderError(
                "TASK_STATE_CONFLICT",
                "Task cancellation did not produce CANCELLED status.",
            )
        return task

    def _registry_context(self) -> AbstractAsyncContextManager[Any]:
        """Create a fresh Registry/session context for one operation."""

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
    def _coerce_intent_spec(
        intent_spec: IntentSpec | dict[str, Any],
    ) -> IntentSpec:
        try:
            return IntentSpec.model_validate(intent_spec)
        except ValidationError as exc:
            raise TaskProviderError(
                "INTENT_SPEC_INVALID",
                "Task creation requires a valid IntentSpec.",
            ) from exc

    @staticmethod
    def _coerce_task_intent(
        intent: TaskIntent | IntentSpec | dict[str, Any],
    ) -> TaskIntent:
        try:
            if isinstance(intent, IntentSpec):
                return to_task_intent(intent)
            if isinstance(intent, TaskIntent):
                return intent.model_copy(deep=True)
            if isinstance(intent, dict):
                relation = str(intent.get("relation", "")).upper()
                if relation in {"CONTINUE", "UPDATE", "CANCEL"}:
                    normalized = dict(intent)
                    normalized["relation"] = {
                        "CONTINUE": "CONTINUE_TASK",
                        "UPDATE": "MODIFY_TASK",
                        "CANCEL": "CANCEL_TASK",
                    }[relation]
                    return TaskIntent.model_validate(normalized)
                if "relation" in intent or "target_task_id" in intent:
                    return TaskIntent.model_validate(intent)
                try:
                    return to_task_intent(IntentSpec.model_validate(intent))
                except ValidationError:
                    return TaskIntent.model_validate(intent)
        except ValidationError as exc:
            raise TaskProviderError(
                "TASK_INTENT_INVALID",
                "Task resolution requires a valid TaskIntent or IntentSpec.",
            ) from exc
        raise TaskProviderError(
            "TASK_INTENT_INVALID",
            "Task resolution requires a TaskIntent or IntentSpec.",
        )

    @staticmethod
    def _in_scope(task: Task, scope: TaskScope) -> bool:
        return (
            task.conversation_id == scope.conversation_id
            and task.user_id == scope.user_id
            and task.tenant_id == scope.tenant_id
        )


__all__ = [
    "TaskBinding",
    "TaskProvider",
    "TaskProviderError",
    "TaskScope",
]
