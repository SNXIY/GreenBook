"""Canonical Task lifecycle manager.

This module is deliberately deterministic.  It persists Tasks, GoalTree
projections, execution references, and plan-version history; it does not
interpret user text and it never calls a tool or worker directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.planning.contracts import PlanRevision

from .models import (
    Task,
    TaskConfirmationState,
    TaskResourceRef,
    TaskRevision,
    TaskRevisionType,
    TaskStatus,
)
from .execution_projection import (
    existing_execution_status,
    is_terminal_execution_status,
    merge_execution_status,
    project_execution_ref,
)
from .repository import (
    TaskRepository,
    TaskRepositoryError,
    TaskVersionConflictError,
)
from .semantic_confirmation import confirmation_identity


class TaskManagerError(RuntimeError):
    """Base error for canonical Task lifecycle operations."""


class TaskNotFoundError(TaskManagerError):
    """The requested Task is not present in the repository."""


class TaskStateTransitionError(TaskManagerError):
    """A caller attempted an invalid deterministic lifecycle transition."""


class TaskConfirmationConflictError(TaskManagerError):
    """A semantic confirmation command lost its Task-level CAS/version check."""


@dataclass(frozen=True, slots=True)
class TaskConfirmationTransition:
    """Result of one typed Task confirmation CAS attempt."""

    task: Task
    changed: bool


_ACTIVE_STATUSES = {
    TaskStatus.CREATED,
    TaskStatus.PLANNING,
    TaskStatus.READY,
    TaskStatus.RUNNING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.WAITING_HUMAN,
    TaskStatus.WAITING_EXTERNAL,
    TaskStatus.PAUSED,
}

_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.CREATED: {
        TaskStatus.PLANNING,
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_HUMAN,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PLANNING: {
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.WAITING_HUMAN,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.READY: {
        TaskStatus.RUNNING,
        TaskStatus.WAITING_HUMAN,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PLANNING,
    },
    TaskStatus.RUNNING: {
        TaskStatus.READY,
        TaskStatus.WAITING_HUMAN,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PLANNING,
    },
    TaskStatus.IN_PROGRESS: {
        TaskStatus.READY,
        TaskStatus.WAITING_HUMAN,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PLANNING,
    },
    TaskStatus.WAITING_HUMAN: {
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.WAITING_EXTERNAL: {
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.FAILED,
    },
    TaskStatus.PAUSED: {
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
    },
    TaskStatus.FAILED: {
        TaskStatus.PLANNING,
        TaskStatus.READY,
        # A Task is an aggregate projection for multiple independent
        # Objectives.  One failed sibling must not prevent a later, explicitly
        # targeted sibling execution from binding to the same Task.
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
    },
    # A completed Task is a completed execution of the current GoalTree, not
    # an immutable conversation record.  A later MODIFY turn may revise the
    # same long-lived Task and submit a new Execution.  CANCELLED remains a
    # terminal administrative decision.
    TaskStatus.COMPLETED: {
        TaskStatus.PLANNING,
        TaskStatus.READY,
        # Reopen for continued execution (a Task may be marked COMPLETED by a
        # run projection while a later ActionLoop resume still needs to bind a
        # queued write to the same durable Task).
        TaskStatus.RUNNING,
    },
    TaskStatus.CANCELLED: set(),
}


class TaskManager:
    """The one canonical entry point for durable Task lifecycle changes."""

    def __init__(self, repository: TaskRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> TaskRepository:
        return self._repository

    async def ensure_storage(self) -> None:
        await self._repository.ensure_storage()

    async def create_task(
        self,
        scope: Any | None = None,
        *,
        conversation_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        root_goal: Goal | str | None = None,
        root_goal_id: str | None = None,
        goal_tree: GoalTree | None = None,
        goal: str = "",
        description: str | None = None,
        goal_category: str = "",
        priority: int = 0,
        task_type: str = "GOAL_DRIVEN",
        execution_mode: str = "AUTO",
        task_id: str | None = None,
    ) -> Task:
        """Create a durable Task and optionally bind its first GoalTree."""

        if scope is not None:
            conversation_id = conversation_id or str(getattr(scope, "conversation_id", ""))
            user_id = user_id or str(getattr(scope, "user_id", ""))
            tenant_id = tenant_id or str(getattr(scope, "tenant_id", ""))

        root = goal_tree.root_goal if goal_tree is not None else root_goal
        description = (
            root.description if isinstance(root, Goal) else str(root or goal)
        ).strip() or str(description or "").strip()
        if not description:
            raise TaskManagerError("A canonical Task requires a root goal.")
        if goal_tree is not None:
            goal_tree.validate_tree()

        task = Task(
            task_id=task_id or str(uuid.uuid4()),
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            goal=description,
            goal_category=goal_category,
            goal_summary=description,
            status=TaskStatus.CREATED,
            priority=priority,
            task_type=task_type,
            execution_mode=execution_mode,
            root_goal_id=(
                root.goal_id if isinstance(root, Goal) else root_goal_id
            ),
        )
        stored = await self._repository.create(task)
        if goal_tree is not None:
            return await self.bind_goal_tree(stored.task_id, goal_tree)
        return stored

    async def get_task(
        self,
        task_id: str,
        *,
        conversation_id: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> Task | None:
        task = await self._repository.get(task_id)
        if task is None:
            return None
        if conversation_id is not None and task.conversation_id != conversation_id:
            return None
        if user_id is not None and task.user_id != user_id:
            return None
        if tenant_id is not None and task.tenant_id != tenant_id:
            return None
        return task

    async def get_required(self, task_id: str, **scope: str) -> Task:
        task = await self.get_task(task_id, **scope)
        if task is None:
            raise TaskNotFoundError(f"Task '{task_id}' was not found in scope.")
        return task

    async def set_confirmation_pending(
        self,
        task_id: str,
        *,
        snapshot_hash: str,
        resume_run_id: str,
        expected_task_version: int | None = None,
    ) -> Task:
        """Persist a Task-level confirmation gate without entering ActionLoop."""

        task = await self.get_required(task_id)
        if (
            expected_task_version is not None
            and task.version != expected_task_version
        ):
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' Task version is stale."
            )
        snapshot_hash = str(snapshot_hash or "")
        if task.confirmation_state == TaskConfirmationState.CONFIRMATION_PENDING:
            if task.confirmation_snapshot_hash == snapshot_hash:
                return task
        elif task.confirmation_state == TaskConfirmationState.CANCELLED:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' cannot enter confirmation from "
                f"{task.confirmation_state.value}."
            )
        previous_version = int(task.confirmation_version or 0)
        if previous_version > 0 and task.confirmation_state in {
            TaskConfirmationState.CONFIRMATION_PENDING,
            TaskConfirmationState.CONFIRMED,
            TaskConfirmationState.SUPERSEDED,
        }:
            task.revisions.append(
                TaskRevision(
                    task_id=task.task_id,
                    type=TaskRevisionType.MODIFY_GOAL,
                    payload={
                        "kind": "SEMANTIC_CONFIRMATION_SUPERSEDED",
                        "previous_confirmation_version": previous_version,
                        "previous_snapshot_hash": task.confirmation_snapshot_hash,
                        "next_snapshot_hash": snapshot_hash,
                    },
                    previous_version=task.version,
                )
            )
        task.requires_confirmation = True
        task.confirmation_state = TaskConfirmationState.CONFIRMATION_PENDING
        task.confirmation_version = max(1, previous_version + 1)
        task.confirmed_version = None
        task.confirmation_snapshot_hash = snapshot_hash or None
        task.confirmation_resume_run_id = str(resume_run_id or "") or None
        task.last_action = "SEMANTIC_CONFIRMATION_PENDING"
        try:
            return await self._persist(task)
        except TaskVersionConflictError as exc:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' changed while entering confirmation."
            ) from exc

    async def auto_admit_task(self, task_id: str) -> Task:
        """Record policy-false admission on the canonical Task."""

        task = await self.get_required(task_id)
        if task.confirmation_state == TaskConfirmationState.AUTO_ADMITTED:
            return task
        if task.confirmation_state == TaskConfirmationState.RESOLVED:
            task.confirmation_state = TaskConfirmationState.AUTO_ADMITTED
            task.requires_confirmation = False
            task.last_action = "SEMANTIC_CONFIRMATION_AUTO_ADMITTED"
            try:
                return await self._persist(task)
            except TaskVersionConflictError as exc:
                raise TaskConfirmationConflictError(
                    f"Task '{task_id}' changed while entering admission."
                ) from exc
        if task.confirmation_state == TaskConfirmationState.CONFIRMED:
            return task
        raise TaskConfirmationConflictError(
            f"Task '{task_id}' cannot be auto-admitted from "
            f"{task.confirmation_state.value}."
        )

    async def confirm_task(
        self,
        task_id: str,
        *,
        expected_confirmation_version: int,
        expected_task_version: int | None = None,
        expected_confirmation_id: str | None = None,
    ) -> Task:
        """Atomically confirm one Task snapshot; repeated same-version confirm is idempotent."""

        transition = await self.confirm_task_transition(
            task_id,
            expected_confirmation_version=expected_confirmation_version,
            expected_task_version=expected_task_version,
            expected_confirmation_id=expected_confirmation_id,
        )
        return transition.task

    async def confirm_task_transition(
        self,
        task_id: str,
        *,
        expected_confirmation_version: int,
        expected_task_version: int | None = None,
        expected_confirmation_id: str | None = None,
    ) -> TaskConfirmationTransition:
        """CAS-confirm a Task and report whether this caller won the CAS."""

        task = await self.get_required(task_id)
        if (
            task.confirmation_state == TaskConfirmationState.CONFIRMED
            and task.confirmed_version == expected_confirmation_version
        ):
            self._validate_confirmation_id(task, expected_confirmation_id, task_id=task_id)
            return TaskConfirmationTransition(task=task, changed=False)
        if task.confirmation_state != TaskConfirmationState.CONFIRMATION_PENDING:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' is not awaiting semantic confirmation."
            )
        if task.confirmation_version != expected_confirmation_version:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' confirmation version is stale."
            )
        if (
            expected_task_version is not None
            and task.version != expected_task_version
        ):
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' Task version is stale."
            )
        self._validate_confirmation_id(task, expected_confirmation_id, task_id=task_id)
        task.confirmation_state = TaskConfirmationState.CONFIRMED
        task.confirmed_version = expected_confirmation_version
        task.last_action = "SEMANTIC_CONFIRMATION_CONFIRMED"
        try:
            return TaskConfirmationTransition(
                task=await self._persist(task),
                changed=True,
            )
        except TaskVersionConflictError as exc:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' changed while confirming."
            ) from exc

    async def cancel_confirmation(
        self,
        task_id: str,
        *,
        expected_confirmation_version: int,
        expected_task_version: int | None = None,
        expected_confirmation_id: str | None = None,
    ) -> Task:
        """Atomically cancel a pending Task; repeated cancellation is idempotent."""

        transition = await self.cancel_confirmation_transition(
            task_id,
            expected_confirmation_version=expected_confirmation_version,
            expected_task_version=expected_task_version,
            expected_confirmation_id=expected_confirmation_id,
        )
        return transition.task

    async def cancel_confirmation_transition(
        self,
        task_id: str,
        *,
        expected_confirmation_version: int,
        expected_task_version: int | None = None,
        expected_confirmation_id: str | None = None,
    ) -> TaskConfirmationTransition:
        """CAS-cancel a Task and report whether this caller won the CAS."""

        task = await self.get_required(task_id)
        if (
            task.confirmation_state == TaskConfirmationState.CANCELLED
            and task.confirmation_version == expected_confirmation_version
        ):
            self._validate_confirmation_id(task, expected_confirmation_id, task_id=task_id)
            return TaskConfirmationTransition(task=task, changed=False)
        if task.confirmation_state != TaskConfirmationState.CONFIRMATION_PENDING:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' is not awaiting semantic confirmation."
            )
        if task.confirmation_version != expected_confirmation_version:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' confirmation version is stale."
            )
        if (
            expected_task_version is not None
            and task.version != expected_task_version
        ):
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' Task version is stale."
            )
        self._validate_confirmation_id(task, expected_confirmation_id, task_id=task_id)
        task.confirmation_state = TaskConfirmationState.CANCELLED
        task.last_action = "SEMANTIC_CONFIRMATION_CANCELLED"
        try:
            return TaskConfirmationTransition(
                task=await self._persist(task),
                changed=True,
            )
        except TaskVersionConflictError as exc:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' changed while cancelling."
            ) from exc

    async def supersede_confirmation(
        self,
        task_id: str,
        *,
        expected_confirmation_version: int | None = None,
        expected_task_version: int | None = None,
        expected_confirmation_id: str | None = None,
    ) -> Task:
        """Make a pending snapshot permanently non-executable for a MODIFY flow."""

        task = await self.get_required(task_id)
        if task.confirmation_state == TaskConfirmationState.SUPERSEDED:
            self._validate_confirmation_request(
                task,
                expected_confirmation_version=expected_confirmation_version,
                expected_task_version=expected_task_version,
                expected_confirmation_id=expected_confirmation_id,
                task_id=task_id,
            )
            return task
        if task.confirmation_state != TaskConfirmationState.CONFIRMATION_PENDING:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' is not awaiting semantic confirmation."
            )
        self._validate_confirmation_request(
            task,
            expected_confirmation_version=expected_confirmation_version,
            expected_task_version=expected_task_version,
            expected_confirmation_id=expected_confirmation_id,
            task_id=task_id,
        )
        task.confirmation_state = TaskConfirmationState.SUPERSEDED
        task.confirmed_version = None
        task.last_action = "SEMANTIC_CONFIRMATION_SUPERSEDED"
        try:
            return await self._persist(task)
        except TaskVersionConflictError as exc:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' changed while superseding."
            ) from exc

    @staticmethod
    def _validate_confirmation_id(
        task: Task,
        expected_confirmation_id: str | None,
        *,
        task_id: str,
    ) -> None:
        if expected_confirmation_id and expected_confirmation_id != confirmation_identity(task):
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' confirmation identity is stale."
            )

    @classmethod
    def _validate_confirmation_request(
        cls,
        task: Task,
        *,
        expected_confirmation_version: int | None,
        expected_task_version: int | None,
        expected_confirmation_id: str | None,
        task_id: str,
    ) -> None:
        if (
            expected_confirmation_version is not None
            and task.confirmation_version != expected_confirmation_version
        ):
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' confirmation version is stale."
            )
        if expected_task_version is not None and task.version != expected_task_version:
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' Task version is stale."
            )
        cls._validate_confirmation_id(task, expected_confirmation_id, task_id=task_id)

    async def get_active_tasks(
        self,
        conversation_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[Task]:
        tasks = await self._repository.list(conversation_id, statuses=tuple(_ACTIVE_STATUSES))
        from .objective_reducer import is_context_isolated_task

        return [
            task for task in tasks
            if not is_context_isolated_task(task)
            and (user_id is None or task.user_id == user_id)
            and (tenant_id is None or task.tenant_id == tenant_id)
        ]

    async def get_resolvable_tasks(
        self,
        conversation_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[Task]:
        """Tasks a follow-up turn may reference, including terminal work.

        A user can keep steering finished work ("再给它补一段…", "刚刚那篇
        正文精简一下"), so delta target resolution must see terminal-but-usable
        tasks.  FAILED remains revisable because its durable resources can be
        the target of a new outcome.  CANCELLED is the one administrative
        terminal state excluded from new work.
        """
        tasks = await self._repository.list(conversation_id, statuses=None)
        from .objective_reducer import is_context_isolated_task

        return [
            task for task in tasks
            if task.status != TaskStatus.CANCELLED
            and not is_context_isolated_task(task)
            and (user_id is None or task.user_id == user_id)
            and (tenant_id is None or task.tenant_id == tenant_id)
        ]

    async def bind_goal_tree(self, task_id: str, goal_tree: GoalTree) -> Task:
        goal_tree.validate_tree()
        task = await self.get_required(task_id)
        if task.status == TaskStatus.CANCELLED:
            raise TaskStateTransitionError(
                f"Cannot bind a GoalTree to terminal Task '{task_id}'."
            )
        previous_goal_tree_version = task.goal_tree_version
        previous_plan_version = task.plan_version
        task.root_goal_id = goal_tree.root_goal.goal_id
        task.goal = goal_tree.root_goal.description or task.goal
        task.goal_summary = task.goal_summary or task.goal
        task.goal_tree_version += 1
        task.goal_tree_snapshot = goal_tree.model_dump(mode="json")
        if task.plan_version == 0:
            task.plan_version = 1
        elif previous_goal_tree_version > 0:
            task.plan_version += 1
            task.plan_history.append(
                PlanRevision(
                    task_id=task.task_id,
                    plan_version=task.plan_version,
                    decision="MODIFY_GOAL",
                    reason="GoalTree revised from a later conversation turn.",
                    observation={
                        "goal_tree_version": task.goal_tree_version,
                        "previous_goal_tree_version": previous_goal_tree_version,
                    },
                    previous_plan_version=previous_plan_version,
                )
            )
        # Objective is the business-goal truth.  Keep the historical GoalTree
        # snapshot and existing ``goals`` projection readable, but do not
        # create new TaskGoal records for a newly bound plan.
        from greenbook_agent_core.task.objective_compat import goals_to_objectives

        # GoalTree is still a compatibility projection.  Rebinding it must
        # not replace the canonical Objective history: a later cross-turn
        # business operation appends a new Goal/Objective while the original
        # terminal Objective keeps its verified resources and lifecycle.
        previous_objectives = {
            str(getattr(item, "objective_id", "")): item
            for item in (getattr(task, "objectives", ()) or ())
            if str(getattr(item, "objective_id", ""))
        }
        projected_objectives = goals_to_objectives(
            goal_tree.all_goals(), task.task_id
        )
        for projected in projected_objectives:
            previous = previous_objectives.get(
                str(getattr(projected, "objective_id", ""))
            )
            if previous is None:
                continue
            # The newly compiled Goal owns the latest desired constraints;
            # historical evidence/ownership is monotonic and is carried
            # forward instead of being reset by the projection.
            merged_constraints = dict(getattr(previous, "constraints", {}) or {})
            merged_constraints.update(
                dict(getattr(projected, "constraints", {}) or {})
            )
            projected.constraints = merged_constraints
            for field in (
                "related_resource_ids",
                "related_artifact_ids",
                "related_operations",
            ):
                old_values = list(getattr(previous, field, ()) or ())
                new_values = list(getattr(projected, field, ()) or ())
                setattr(
                    projected,
                    field,
                    list(dict.fromkeys([*old_values, *new_values])),
                )
            previous_status = getattr(previous, "status", None)
            previous_status_value = str(
                getattr(previous_status, "value", previous_status) or ""
            ).upper()
            projected_status_value = str(
                getattr(getattr(projected, "status", None), "value", getattr(projected, "status", ""))
                or ""
            ).upper()
            if previous_status_value in {"COMPLETED", "FAILED", "SUPERSEDED", "WAITING"} and (
                projected_status_value == "PENDING"
                or previous_status_value in {"COMPLETED", "FAILED", "SUPERSEDED"}
            ):
                projected.status = previous_status
                projected.completed_at = getattr(previous, "completed_at", None)
            projected.updated_at = getattr(previous, "updated_at", projected.updated_at)
        # GoalTree is a compatibility projection and may not contain the
        # Objective-first mutation allocated by a later cross-turn turn.
        # Preserve such canonical Objectives instead of letting a legacy
        # rebind erase their new outcome/resource lineage.
        projected_ids = {
            str(getattr(item, "objective_id", ""))
            for item in projected_objectives
        }
        task.objectives = projected_objectives + [
            item
            for objective_id, item in previous_objectives.items()
            if objective_id and objective_id not in projected_ids
        ]
        task.revisions.append(
            TaskRevision(
                task_id=task.task_id,
                type=TaskRevisionType.MODIFY_GOAL,
                payload={
                    "goal_tree_version": task.goal_tree_version,
                    "root_goal_id": task.root_goal_id,
                },
                previous_version=task.version,
            )
        )
        if task.status in {
            TaskStatus.CREATED,
            TaskStatus.PLANNING,
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
        }:
            task.status = self._transition_value(task.status, TaskStatus.READY)
            task.phase = "READY"
            task.completed_at = None
            task.last_error = None
        return await self._persist(task)

    async def modify_task(
        self,
        task_id: str,
        *,
        goal_tree: GoalTree | None = None,
        **changes: Any,
    ) -> Task:
        if goal_tree is not None:
            task = await self.bind_goal_tree(task_id, goal_tree)
            if not changes:
                return task
        task = await self.get_required(task_id)
        allowed = {"goal", "goal_summary", "priority", "task_type", "execution_mode", "depends_on"}
        unknown = set(changes) - allowed
        if unknown:
            raise TaskManagerError(f"Unsupported Task fields: {sorted(unknown)}")
        for name, value in changes.items():
            setattr(task, name, value)
        task.revisions.append(
            TaskRevision(
                task_id=task.task_id,
                type=TaskRevisionType.MODIFY_GOAL,
                payload={key: value for key, value in changes.items()},
                previous_version=task.version,
            )
        )
        return await self._persist(task)

    async def pause_task(self, task_id: str, *, reason: str = "") -> Task:
        task = await self._transition(task_id, TaskStatus.PAUSED)
        task.last_action = "PAUSE"
        if reason:
            task.action_history.append(reason)
        return await self._persist(task)

    async def resume_task(
        self,
        task_id: str,
        *,
        expected_confirmation_version: int | None = None,
        expected_task_version: int | None = None,
        expected_confirmation_id: str | None = None,
    ) -> Task:
        task = await self.get_required(task_id)
        if expected_confirmation_version is not None:
            if (
                task.confirmation_state != TaskConfirmationState.CONFIRMED
                or task.confirmed_version != expected_confirmation_version
            ):
                raise TaskConfirmationConflictError(
                    f"Task '{task_id}' confirmation is stale."
                )
            self._validate_confirmation_request(
                task,
                expected_confirmation_version=expected_confirmation_version,
                expected_task_version=expected_task_version,
                expected_confirmation_id=expected_confirmation_id,
                task_id=task_id,
            )
        if task.requires_confirmation and (
            task.confirmation_state != TaskConfirmationState.CONFIRMED
            or task.confirmed_version != task.confirmation_version
        ):
            raise TaskConfirmationConflictError(
                f"Task '{task_id}' is not confirmed for execution."
            )
        # RUNNING is reserved for a Task backed by live Execution work.  A
        # terminal predecessor can leave another Objective pending, but the
        # durable Task must pass through READY until the next Execution is
        # actually bound; this prevents RUNNING + no active Execution residue.
        from .objective_reducer import has_nonterminal_execution

        if task.status == TaskStatus.RUNNING and (
            task.active_execution_id or has_nonterminal_execution(task)
        ):
            target = TaskStatus.RUNNING
        elif task.status == TaskStatus.RUNNING:
            target = TaskStatus.READY
        else:
            target = TaskStatus.RUNNING if task.active_execution_id else TaskStatus.READY
        task.status = self._transition_value(task.status, target)
        task.last_action = "RESUME"
        task.last_error = None
        try:
            return await self._persist(task)
        except TaskVersionConflictError as exc:
            if expected_confirmation_version is not None:
                raise TaskConfirmationConflictError(
                    f"Task '{task_id}' changed while resuming confirmation."
                ) from exc
            raise

    async def cancel_task(self, task_id: str, *, reason: str = "") -> Task:
        task = await self._transition(task_id, TaskStatus.CANCELLED)
        task.last_action = "CANCEL"
        if reason:
            task.action_history.append(reason)
        return await self._persist(task)

    async def wait_for_human(self, task_id: str, *, reason: str = "", goal_id: str = "") -> Task:
        """Persist a user clarification boundary without stopping siblings.

        Only the Goal identified by ``goal_id`` is paused (independent sibling
        Goals stay runnable and are not reported as waiting).  When ``goal_id``
        is empty — single-Goal tasks and legacy callers — every executable
        Goal is marked WAITING_USER so the Task surfaces the wait.
        """
        task = await self.get_required(task_id)
        task.status = self._transition_value(task.status, TaskStatus.WAITING_HUMAN)
        task.phase = "WAITING_USER"
        task.last_action = "WAITING_USER"
        task.last_error = reason or None
        from .objective_reducer import mutation_objective_is_superseded

        for objective in task.objectives:
            if mutation_objective_is_superseded(objective):
                continue
            if goal_id and objective.objective_id != goal_id:
                continue
            objective.status = "WAITING"
        return await self._persist(task)

    async def complete_task(self, task_id: str, *, result: Mapping[str, Any] | None = None) -> Task:
        task = await self.get_required(task_id)
        objective_contract = any(
            bool(getattr(objective, "required_capabilities", None))
            or bool(getattr(objective, "expected_resource_kind", ""))
            or any(
                bool(value)
                for value in dict(
                    getattr(objective, "expected_postcondition", None) or {}
                ).values()
            )
            for objective in task.objectives
        )
        if task.objectives and objective_contract:
            # Task terminal-success is an aggregate projection, not a caller
            # assertion.  Recompute from the canonical evidence reducer and
            # reject a direct completion while any required Objective is not
            # verified-success.
            from .objective_reducer import (
                ObjectiveStateReducer,
                all_objectives_satisfied,
            )

            ObjectiveStateReducer().reduce(task)
            if not all_objectives_satisfied(task):
                raise TaskStateTransitionError(
                    f"Task '{task_id}' cannot complete before all Objectives "
                    "are verified successfully."
                )
        task.status = self._transition_value(task.status, TaskStatus.COMPLETED)
        task.last_action = "COMPLETE"
        task.completed_at = _now()
        if result:
            task.action_history.append(str(dict(result)))
        return await self._persist(task)

    async def fail_task(self, task_id: str, *, error: str, retryable: bool = False) -> Task:
        task = await self._transition(task_id, TaskStatus.FAILED)
        task.last_action = "FAIL"
        task.last_error = error
        task.retry_count += 1
        if retryable and task.retry_count <= task.max_retries:
            task.action_history.append("REPLAN_REQUIRED")
        return await self._persist(task)

    async def bind_execution(
        self,
        task_id: str,
        execution_id: str,
        *,
        goal_id: str | None = None,
        status: str = "SUBMITTED",
    ) -> Task:
        task = await self.get_required(task_id)
        # Monotonic execution-state projection: a terminal execution is a latch
        # and is never rebound as active work.  A late QUEUED/RUNNING update for
        # an already-terminal execution must not regress it.
        effective = merge_execution_status(
            existing_execution_status(task.execution_refs, execution_id),
            status,
        )
        task.execution_refs = project_execution_ref(
            task.execution_refs,
            execution_id=execution_id,
            task_id=task.task_id,
            goal_id=goal_id,
            status=status,
        )
        if is_terminal_execution_status(effective):
            if task.active_execution_id == execution_id:
                task.active_execution_id = None
        else:
            task.active_execution_id = execution_id
            task.status = self._transition_value(task.status, TaskStatus.RUNNING)
            task.last_action = "BIND_EXECUTION"
        return await self._persist(task)

    async def add_resource(
        self,
        task_id: str,
        *,
        resource_id: str,
        resource_kind: str,
        title: str = "",
        status: str = "",
        objective_id: str | None = None,
    ) -> Task:
        """Durably bind a real resource to a Task's resource_index (dedupe by id)."""
        task = await self.get_required(task_id)
        existing = {
            (str(r.resource_id), str(r.resource_kind or "").upper())
            for r in task.resource_index
        }
        resource_key = (str(resource_id), str(resource_kind or "").upper())
        owner_conflict = False
        if resource_key not in existing:
            task.resource_index.append(
                TaskResourceRef(
                    resource_id=str(resource_id),
                    resource_kind=str(resource_kind),
                    objective_id=str(objective_id) if objective_id else None,
                    title=title or None,
                    status=status or None,
                )
            )
        else:
            # A replay may carry fresher ownership/title/status facts for the
            # same typed resource.  Preserve the single typed row while
            # allowing the durable owner to be completed below.
            for resource in task.resource_index:
                if (
                    str(resource.resource_id),
                    str(resource.resource_kind or "").upper(),
                ) == resource_key:
                    owner_conflict = bool(
                        resource.objective_id
                        and objective_id
                        and str(resource.objective_id) != str(objective_id)
                    )
                    if objective_id and not resource.objective_id:
                        resource.objective_id = str(objective_id)
                    if title and not resource.title:
                        resource.title = title
                    if status and not resource.status:
                        resource.status = status
                    break
        # Ownership: bind the verified resource to the Objective that initiated
        # the execution (durable correlation).  Only when objective_id matches an
        # existing Objective; never guess the current/active Objective.
        if objective_id and not owner_conflict:
            for objective in getattr(task, "objectives", ()) or ():
                if str(getattr(objective, "objective_id", "")) != objective_id:
                    continue
                owned = list(getattr(objective, "related_resource_ids", ()) or ())
                if str(resource_id) not in owned:  # dedupe (replay-safe)
                    owned.append(str(resource_id))
                objective.related_resource_ids = owned
                break
        task.last_action = "ADD_RESOURCE"
        return await self._persist(task)

    async def record_replan(
        self,
        task_id: str,
        *,
        decision: str,
        observation: Mapping[str, Any] | None = None,
        reason: str = "",
    ) -> Task:
        task = await self.get_required(task_id)
        previous = task.plan_version
        task.plan_version = previous + 1
        task.plan_history.append(
            PlanRevision(
                task_id=task.task_id,
                plan_version=task.plan_version,
                decision=decision,
                reason=reason,
                observation=dict(observation or {}),
                previous_plan_version=previous or None,
            )
        )
        task.revisions.append(
            TaskRevision(
                task_id=task.task_id,
                type=TaskRevisionType.REPLAN,
                payload={
                    "decision": decision,
                    "plan_version": task.plan_version,
                    "reason": reason,
                },
                previous_version=task.version,
            )
        )
        if task.status in {TaskStatus.RUNNING, TaskStatus.FAILED, TaskStatus.READY}:
            task.status = self._transition_value(task.status, TaskStatus.PLANNING)
        return await self._persist(task)

    async def preempt_for(self, active_task_id: str, incoming_priority: int) -> Task:
        task = await self.get_required(active_task_id)
        if incoming_priority <= task.priority:
            return task
        if task.status in {TaskStatus.RUNNING, TaskStatus.IN_PROGRESS, TaskStatus.READY}:
            return await self.pause_task(task.task_id, reason="PREEMPTED_BY_HIGHER_PRIORITY_TASK")
        return task

    async def _transition(self, task_id: str, target: TaskStatus) -> Task:
        task = await self.get_required(task_id)
        task.status = self._transition_value(task.status, target)
        return task

    async def _persist(self, task: Task) -> Task:
        # Central invariant: RUNNING is a live-execution projection, never a
        # label for a detached historical predecessor.  Keep approval/waiting
        # refs live; only the fully detached shape is normalized to READY.
        from .objective_reducer import has_nonterminal_execution

        if (
            task.status == TaskStatus.RUNNING
            and not task.active_execution_id
            and not has_nonterminal_execution(task)
        ):
            task.status = TaskStatus.READY
        task.updated_at = _now()
        try:
            return await self._repository.update(task, expected_version=task.version)
        except TaskRepositoryError:
            raise
        except Exception as exc:
            raise TaskManagerError("Task persistence failed.") from exc

    @staticmethod
    def _transition_value(current: TaskStatus, target: TaskStatus) -> TaskStatus:
        if current == target:
            return target
        if target not in _TRANSITIONS.get(current, set()):
            raise TaskStateTransitionError(
                f"Invalid Task transition: {current.value} -> {target.value}."
            )
        return target




def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "TaskManager",
    "TaskConfirmationConflictError",
    "TaskConfirmationTransition",
    "TaskManagerError",
    "TaskNotFoundError",
    "TaskStateTransitionError",
]
