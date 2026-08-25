"""Scoped Task persistence boundary for the canonical TaskManager.

This module is an API/storage adapter only.  It does not understand user
messages, Goals, plans, or execution control semantics.  Lifecycle
mutations belong to ``TaskManager``; terminal execution projections are the
one read-model write retained here for the completion publisher.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from inspect import isawaitable
from typing import Any

from greenbook_agent_core.db.connection import session_ctx
from greenbook_agent_core.task.execution_projection import (
    is_terminal_execution_status,
    project_execution_ref,
)
from greenbook_agent_core.task.models import (
    ArtifactRef,
    Task,
    TaskResourceRef,
    TaskStatus,
)
from greenbook_agent_core.task.objective_reducer import has_nonterminal_execution
from greenbook_agent_core.task.registry import TaskRegistry
from greenbook_agent_core.task.repository import (
    TaskRepositoryError,
    TaskVersionConflict,
)

_TASK_PROJECTION_LOCKS: dict[str, asyncio.Lock] = {}


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
        except ValueError:
            # A detached write carries no owning Task; a non-UUID id is not a
            # query failure, it simply has no Task projection to update.
            return None
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
        """Persist a read-model snapshot without changing lifecycle state.

        Execution refs are merged monotonically against the currently persisted
        Task, so a stale in-memory snapshot can never regress an execution that
        has since gone terminal.
        """

        scope = self._coerce_scope(scope)
        if not self._in_scope(task, scope):
            raise TaskProviderError("TASK_SCOPE_MISMATCH", "Task is outside scope.")
        current = await self.get_task(scope, task.task_id)
        now = datetime.now(UTC)
        projection_refs = [item.model_copy(deep=True) for item in (current.execution_refs if current else ())]
        for ref in task.execution_refs:
            projection_refs = project_execution_ref(
                projection_refs,
                execution_id=ref.execution_id,
                task_id=task.task_id,
                goal_id=ref.goal_id,
                status=ref.status,
                now=now,
            )
        try:
            async with self._registry_context() as registry:
                result = registry.update_task(
                    task.task_id,
                    expected_version=task.version,
                    goals=[item.model_dump(mode="json") for item in task.goals],
                    revisions=[item.model_dump(mode="json") for item in task.revisions],
                    execution_refs=[
                        item.model_dump(mode="json") for item in projection_refs
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
        goal_id: str = "",
        objective_id: str | None = None,
    ) -> Task | None:
        """Serialize convergence for one Task, not for its Conversation."""

        task_lock = _TASK_PROJECTION_LOCKS.setdefault(str(task_id), asyncio.Lock())
        async with task_lock:
            for attempt in range(3):
                try:
                    return await self._persist_completion_projection_locked(
                        scope,
                        task_id=task_id,
                        execution_id=execution_id,
                        status=status,
                        artifacts=artifacts,
                        error=error,
                        goal_id=goal_id,
                        objective_id=objective_id,
                    )
                except TaskVersionConflict:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0)
            return None

    async def _persist_completion_projection_locked(
        self,
        scope: TaskScope,
        *,
        task_id: str,
        execution_id: str,
        status: str,
        artifacts: Sequence[dict[str, Any]],
        error: str | None = None,
        goal_id: str = "",
        objective_id: str | None = None,
    ) -> Task | None:
        """Project terminal Execution facts into the Task read model.

        A terminal Execution alone does not complete its Task. In incremental
        mode each Execution carries exactly one semantic action for one Goal;
        that Goal is completed only when its desired business state is
        satisfied by real artifacts, and the Task only when every executable
        Goal is satisfied. Whole-plan Executions (no matching goal_id) keep
        the legacy projection: the compiled GoalTree ran as a whole.
        """

        scope = self._coerce_scope(scope)
        task = await self.get_task(scope, task_id)
        if task is None:
            return None
        # Storage adapters may return a Task projection assembled from plain
        # JSON mappings (for example, lightweight in-memory registries used by
        # integration tests).  Re-validate at this boundary so reducers and
        # subsequent mutations always receive the declared nested models.
        if isinstance(task, Task):
            task = Task.model_validate(task.__dict__)
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
                existing_resource = next(
                    (
                        item for item in resource_index
                        if item.resource_id == resource_id
                        and str(item.resource_kind or "").upper() == resource_kind.upper()
                    ),
                    None,
                )
                effective_objective_id = objective_id
                if (
                    existing_resource is not None
                    and existing_resource.objective_id
                    and objective_id
                    and str(existing_resource.objective_id) != str(objective_id)
                ):
                    effective_objective_id = existing_resource.objective_id
                resource = TaskResourceRef(
                    resource_id=resource_id,
                    resource_kind=resource_kind,
                    objective_id=str(effective_objective_id) if effective_objective_id else None,
                    title=_optional_string(raw.get("title")),
                    status=_optional_string(raw.get("status")),
                    scheduled_at=_optional_string(raw.get("run_at")),
                )
                resource_index = [
                    item for item in resource_index
                    if not (
                        item.resource_id == resource_id
                        and str(item.resource_kind or "").upper() == resource_kind.upper()
                    )
                ]
                resource_index.append(resource)

        # Objective ownership: bind every resource produced by this Execution to
        # the Objective that initiated it.  objective_id comes from the persisted
        # PlanExecution (the initiating Objective), never from the current/active
        # Objective, so an Execution started under Objective A still owns its
        # resource when the turn has since switched to Objective B.  A missing or
        # non-matching objective_id binds nothing (never guess the owner).
        objectives = list(task.objectives)
        if objective_id:
            owned_resources = []
            for raw in artifacts:
                rid = _optional_string(raw.get("resource_id"))
                if not rid:
                    continue
                kind = str(
                    raw.get("resource_type")
                    or _resource_type_from_artifact(str(raw.get("type") or raw.get("artifact_type") or ""))
                    or ""
                ).upper()
                ref = next(
                    (
                        item for item in resource_index
                        if item.resource_id == rid
                        and (not kind or str(item.resource_kind or "").upper() == kind)
                    ),
                    None,
                )
                if ref is None or not ref.objective_id or str(ref.objective_id) == str(objective_id):
                    owned_resources.append(rid)
            for objective in objectives:
                if str(getattr(objective, "objective_id", "")) != objective_id:
                    continue
                related = list(getattr(objective, "related_resource_ids", ()) or ())
                for rid in owned_resources:
                    if rid not in related:
                        related.append(rid)
                objective.related_resource_ids = related
                break

        execution_refs = project_execution_ref(
            task.execution_refs,
            execution_id=execution_id,
            task_id=task_id,
            # Preserve the initiating Objective correlation on the existing
            # TaskExecutionRef.  The reducer uses this field to keep one
            # failed/active execution from changing sibling Objectives.
            goal_id=objective_id or None,
            status=normalized_status,
            now=now,
        )
        # Use the newly projected execution facts for every compatibility path
        # below.  A terminal callback must never leave a detached Task looking
        # RUNNING when no other execution is live.
        task.execution_refs = execution_refs

        # New tasks are Objective-owned.  Recompute their business status from
        # verified resources/artifacts and execution facts without materializing
        # a parallel TaskGoal projection.
        objective_projection = list(task.objectives)
        if objective_projection and not task.goals:
            task.resource_index = resource_index
            task.execution_refs = execution_refs
            from .objective_reducer import (
                ObjectiveStateReducer,
                bind_related,
                mutation_objective_is_superseded,
            )

            # The ActionLoop's direct completion path already binds a
            # successful mutation to its Objective through the existing
            # operation correlation.  Queue completion must make the same
            # binding before the reducer runs; otherwise a completed mutation
            # with no returned artifact (for example DELETE_POST or
            # CANCEL_SCHEDULE) remains PENDING and the resumed loop submits
            # the same already-verified mutation again.  Keep the
            # artifact-less allowance limited to capabilities whose Java
            # postcondition is deletion/cancellation; a generic mutation
            # without evidence must remain pending.
            binding_objective_id = str(objective_id or goal_id)
            binding_objective = next(
                (
                    item
                    for item in task.objectives
                    if str(getattr(item, "objective_id", "")) == binding_objective_id
                ),
                None,
            )
            artifactless_capabilities = {
                "DELETE_POST",
                "CANCEL_SCHEDULE",
            }
            allow_artifactless = bool(
                binding_objective is not None
                and set(getattr(binding_objective, "required_capabilities", ()) or ())
                & artifactless_capabilities
            )
            if (
                normalized_status == "COMPLETED"
                and (artifacts or allow_artifactless)
                and binding_objective_id
            ):
                bind_related(
                    task,
                    objective_id=binding_objective_id,
                    operation_id=execution_id,
                )

            objectives = ObjectiveStateReducer().reduce(task)
            from .objective_reducer import all_objectives_satisfied

            # A terminal Execution is only one settled step.  The Task becomes
            # terminal after every Objective is satisfied and no execution ref
            # remains non-terminal; this is the single reducer authority.
            if all_objectives_satisfied(task):
                projection_status = TaskStatus.COMPLETED
            elif any(
                str(item.status) == "FAILED"
                and not mutation_objective_is_superseded(item)
                for item in objectives
            ):
                projection_status = TaskStatus.FAILED
            elif task.status == TaskStatus.FAILED and any(
                not mutation_objective_is_superseded(item)
                and str(item.status) not in {"COMPLETED", "FAILED", "CANCELLED", "SUPERSEDED"}
                for item in objectives
            ):
                # Converge Tasks persisted by the old FAILED placeholder once
                # the marker proves that the only historical failure was a
                # pre-submit supersede.
                projection_status = TaskStatus.RUNNING
            else:
                projection_status = task.status
            projection_completed_at = (
                now if projection_status == TaskStatus.COMPLETED else task.completed_at
            )
            projection_error = error if normalized_status == "FAILED" else task.last_error
            projection_active_execution_id = (
                None
                if is_terminal_execution_status(normalized_status)
                and task.active_execution_id == execution_id
                else task.active_execution_id
            )
            if (
                projection_status == TaskStatus.RUNNING
                and projection_active_execution_id is None
                and not has_nonterminal_execution(task)
            ):
                # READY means the Task may be resumed/selected, while RUNNING
                # is reserved for work backed by a live Execution reference.
                projection_status = TaskStatus.READY
            try:
                async with self._registry_context() as registry:
                    result = registry.update_task(
                        task_id,
                        expected_version=task.version,
                        status=projection_status,
                        goals=[],
                        objectives=[item.model_dump(mode="json") for item in objectives],
                        artifacts=[item.model_dump(mode="json") for item in artifact_refs],
                        execution_refs=[item.model_dump(mode="json") for item in execution_refs],
                        active_execution_id=projection_active_execution_id,
                        resource_index=[item.model_dump(mode="json") for item in resource_index],
                        last_error=projection_error,
                        completed_at=(
                            projection_completed_at.isoformat()
                            if hasattr(projection_completed_at, "isoformat")
                            else projection_completed_at
                        ),
                    )
                    return await result if isawaitable(result) else result
            except TaskVersionConflict:
                raise
            except Exception as exc:
                raise TaskProviderError(
                    "TASK_COMPLETION_PROJECTION_PERSIST_FAILED",
                    "The terminal Execution projection could not be written.",
                ) from exc

        goals = list(task.goals)
        terminal = normalized_status in {"COMPLETED", "FAILED", "CANCELLED"}
        goal_tree = _goal_tree_from_task(task)
        owns_goal = bool(
            goal_id
            and goal_tree is not None
            and any(item.goal_id == goal_id for item in goal_tree.executable_goals())
        )
        if owns_goal and terminal:
            # Incremental mode: one Execution is one semantic action for one
            # Goal.  active_execution_id is only a compatibility projection;
            # it must not discard an out-of-order sibling completion.
            # Satisfaction observes the Goal's own durable facts: the task's
            # already-persisted artifacts (previous executions) plus the new
            # Execution's artifacts, filtered per-Goal.
            persisted_artifacts = [
                {
                    **item.model_dump(mode="json"),
                    "persisted": "true",
                }
                for item in task.artifacts
                if item.resource_id or item.artifact_id
            ]
            goals = self._project_incremental_goal(
                goals,
                goal_id=goal_id,
                goal_tree=goal_tree,
                status=normalized_status,
                execution_id=execution_id,
                resource_index=resource_index,
                artifacts=[*persisted_artifacts, *artifacts],
                now=now,
            )
            projection_status = self._aggregate_task_status(
                goals,
                task.status,
                executable_goal_ids={
                    item.goal_id for item in goal_tree.executable_goals()
                },
            )
        elif (
            not goal_id
            and terminal
            and not task.objectives
            and task.active_execution_id in {None, "", execution_id}
        ):
            # Historical whole-plan recovery projection only. New requests
            # are prevented from producing this shape at the adapter boundary.
            # Objective-based Tasks are driven by Objective satisfaction, never
            # by a single no-goal_id Execution: one Objective's completion must
            # not mark the whole multi-objective Task COMPLETED (that would
            # terminate JVM/Spring objectives after only the first one ran).
            for goal in goals:
                goal.status = normalized_status
                goal.execution_id = execution_id
                goal.updated_at = now.isoformat()
            projection_status = terminal_status
        else:
            projection_status = task.status
        projection_completed_at = (
            now
            if projection_status
            in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
            else task.completed_at
        )
        projection_goals = goals
        projection_error = error if normalized_status == "FAILED" else task.last_error
        # A terminal Execution is no longer active work: clear the active
        # pointer when it pointed at exactly this Execution (never another).
        projection_active_execution_id = task.active_execution_id
        if is_terminal_execution_status(normalized_status) and task.active_execution_id == execution_id:
            projection_active_execution_id = None
        if (
            projection_status == TaskStatus.RUNNING
            and projection_active_execution_id is None
            and not has_nonterminal_execution(task)
        ):
            projection_status = TaskStatus.READY

        try:
            async with self._registry_context() as registry:
                result = registry.update_task(
                    task_id,
                    expected_version=task.version,
                    status=projection_status,
                    goals=[item.model_dump(mode="json") for item in projection_goals],
                    objectives=[item.model_dump(mode="json") for item in objectives],
                    artifacts=[item.model_dump(mode="json") for item in artifact_refs],
                    execution_refs=[item.model_dump(mode="json") for item in execution_refs],
                    active_execution_id=projection_active_execution_id,
                    resource_index=[item.model_dump(mode="json") for item in resource_index],
                    last_error=projection_error,
                    completed_at=(
                        projection_completed_at.isoformat()
                        if hasattr(projection_completed_at, "isoformat")
                        else projection_completed_at
                    ),
                )
                return await result if isawaitable(result) else result
        except TaskVersionConflict:
            raise
        except Exception as exc:
            raise TaskProviderError(
                "TASK_COMPLETION_PROJECTION_PERSIST_FAILED",
                "The terminal Execution projection could not be written.",
            ) from exc

    @staticmethod
    def _project_incremental_goal(
        goals: Sequence[Any],
        *,
        goal_id: str,
        goal_tree: Any,
        status: str,
        execution_id: str,
        resource_index: Sequence[Any],
        artifacts: Sequence[dict[str, Any]],
        now: datetime,
    ) -> list[Any]:
        """Update only the Execution's own Goal from real business facts."""

        from greenbook_agent_core.goal.satisfaction import (
            goal_is_satisfied,
            publication_intent_of,
        )

        goal_models = {
            str(item.goal_id): item
            for item in goal_tree.executable_goals()
        }
        target = goal_models.get(goal_id)
        # Per-Goal satisfaction must only observe business facts owned by this
        # Goal.  This Execution's own artifacts are declared for this Goal by
        # the caller (goal_id argument): they are always counted.  Persisted
        # task artifacts from other Executions are attributed by their
        # goal_id / step_id, or excluded when they carry no ownership signal,
        # so a sibling Goal's draft/schedule never satisfies this Goal.
        single_goal = len(goal_models) == 1
        owned_artifacts = _owned_artifacts_for_goal(
            artifacts,
            goal_id=goal_id,
            single_goal=single_goal,
        )
        facts = _resource_facts(resource_index, owned_artifacts)
        # A Scheduled-Publish Goal is satisfied by the Schedule it created
        # plus the Draft it schedules.  The Draft is owned by the producing
        # Goal (GENERATE_CONTENT), so it lives in the Task's persisted
        # resource index, not in this Execution's own artifacts — without it
        # the schedule Goal would stay IN_PROGRESS forever and the Task never
        # reach COMPLETED even after the schedule was durably created.
        if (
            not facts.get("draft_id")
            and target is not None
            and publication_intent_of(target) == "SCHEDULED_PUBLISH"
        ):
            for resource in resource_index:
                if (
                    str(getattr(resource, "resource_kind", "") or "").upper() == "DRAFT"
                    and getattr(resource, "resource_id", None)
                ):
                    facts = {**facts, "draft_id": str(resource.resource_id)}
                    break
        satisfied = bool(
            target is not None
            and status == "COMPLETED"
            and goal_is_satisfied(target, facts)
        )
        projected = list(goals)
        for goal in projected:
            if goal.goal_id != goal_id:
                continue
            for raw in artifacts:
                artifact_id = str(raw.get("artifact_id") or "")
                if not artifact_id:
                    continue
                if not any(item.artifact_id == artifact_id for item in goal.artifact_refs):
                    goal.artifact_refs.append(ArtifactRef(
                        artifact_id=artifact_id,
                        task_id=goal.task_id,
                        step_id=str(raw.get("step_id") or ""),
                        artifact_type=str(raw.get("artifact_type") or raw.get("type") or ""),
                        resource_id=_optional_string(raw.get("resource_id")),
                        resource_kind=_optional_string(raw.get("resource_type")),
                        summary=_optional_string(raw.get("summary")),
                    ))
            if status in {"FAILED", "CANCELLED"}:
                goal.status = status
            elif satisfied:
                goal.status = "COMPLETED"
            else:
                # Action completed but the Goal's desired business state is
                # still missing (e.g. a Scheduled Goal with only a Draft).
                goal.status = "IN_PROGRESS"
            goal.execution_id = execution_id
            goal.updated_at = now.isoformat()
        return projected

    @staticmethod
    def _aggregate_task_status(
        goals: Sequence[Any],
        current_status: TaskStatus,
        executable_goal_ids: set[str] | None = None,
    ) -> TaskStatus:
        """Task terminal state requires every executable Goal terminal."""

        relevant_goals = [
            goal
            for goal in goals
            if executable_goal_ids is None or goal.goal_id in executable_goal_ids
        ]
        if not relevant_goals:
            return current_status
        if any(
            str(goal.status) in {"FAILED", "CANCELLED"}
            for goal in relevant_goals
        ):
            return TaskStatus.FAILED
        if all(str(goal.status) == "COMPLETED" for goal in relevant_goals):
            return TaskStatus.COMPLETED
        return TaskStatus.RUNNING

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
            fields = _task_storage_fields(task)
            if expected_version is None:
                result = registry.update_task(task.task_id, **fields)
            else:
                result = registry.update_task(
                    task.task_id,
                    expected_version=expected_version,
                    **fields,
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


def _goal_tree_from_task(task: Any):
    """Rebuild the canonical GoalTree from the durable task snapshot."""

    snapshot = getattr(task, "goal_tree_snapshot", None) or {}
    if not snapshot:
        return None
    try:
        from greenbook_agent_core.goal.models import GoalTree

        return GoalTree.model_validate(snapshot)
    except Exception:
        return None


def _artifact_owned_by_goal(
    raw: Mapping[str, Any],
    goal_id: str,
    *,
    single_goal: bool = False,
) -> bool:
    """Return whether an artifact belongs to ``goal_id``.

    Preference order: explicit ``goal_id`` field, then the first segment of a
    ``step_id`` convention ("g3:reasoning" -> "g3").  Artifacts with neither
    are attributed to the sole executable Goal in single-Goal tasks (the
    historical whole-task shape) and excluded from per-Goal satisfaction in
    multi-Goal tasks, so one Goal's resource cannot satisfy another Goal.
    """
    explicit = str(raw.get("goal_id") or "")
    if explicit:
        return explicit == goal_id
    step_id = str(raw.get("step_id") or "")
    if ":" in step_id:
        return step_id.split(":", 1)[0] == goal_id
    return single_goal


def _owned_artifacts_for_goal(
    artifacts: Sequence[dict[str, Any]],
    *,
    goal_id: str,
    single_goal: bool,
) -> list[dict[str, Any]]:
    """Return the artifacts that belong to ``goal_id`` for satisfaction.

    The caller passes the Execution's own artifacts (declared for this Goal by
    the goal_id argument) merged with the task's persisted artifacts.  An
    artifact already persisted under this task is historical; historical
    artifacts are attributed by goal_id / step_id ownership and excluded when
    they carry none.  Artifacts not yet persisted are this Execution's own and
    count for the declared Goal.
    """
    owned: list[dict[str, Any]] = []
    for raw in artifacts:
        if not isinstance(raw, Mapping):
            continue
        artifact_id = str(raw.get("artifact_id") or "")
        # Historical persisted artifact: require an explicit ownership signal.
        if artifact_id and str(raw.get("persisted") or "") == "true":
            if _artifact_owned_by_goal(raw, goal_id, single_goal=single_goal):
                owned.append(dict(raw))
            continue
        owned.append(dict(raw))
    return owned


def _resource_facts(
    resource_index: Sequence[Any],
    artifacts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate real business resources (Draft/Schedule/Post) for a Goal.

    The returned ``draft_id`` / ``schedule_id`` / ``post_id`` values are the
    concrete external resource ids owned by the Goal, never boolean
    placeholders: a Goal is only satisfied by its own resources.
    """

    draft_id = ""
    schedule_id = ""
    post_id = ""
    artifact_types: set[str] = set()
    completed_capabilities: set[str] = set()
    for raw in artifacts:
        artifact_type = str(raw.get("artifact_type") or raw.get("type") or "").upper()
        if artifact_type:
            artifact_types.add(artifact_type)
        capability = str(raw.get("capability") or "").upper()
        if capability:
            completed_capabilities.add(capability)
        kind = str(
            raw.get("resource_type")
            or _resource_type_from_artifact(artifact_type)
            or ""
        ).upper()
        resource_id = _optional_string(raw.get("resource_id"))
        if kind == "DRAFT" and resource_id:
            draft_id = resource_id
        elif kind == "SCHEDULE" and resource_id:
            schedule_id = resource_id
        elif kind == "POST" and resource_id:
            post_id = resource_id
    return {
        "draft_id": draft_id,
        "schedule_id": schedule_id,
        "post_id": post_id,
        "artifact_types": artifact_types,
        "completed_capabilities": completed_capabilities,
    }


__all__ = [
    "TaskProvider",
    "TaskProviderRepository",
    "TaskProviderError",
    "TaskScope",
]
