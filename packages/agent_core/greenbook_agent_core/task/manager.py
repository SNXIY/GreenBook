"""Canonical Task lifecycle manager.

This module is deliberately deterministic.  It persists Tasks, GoalTree
projections, execution references, and plan-version history; it does not
interpret user text and it never calls a tool or worker directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.planning.contracts import PlanRevision

from .models import (
    Task,
    TaskExecutionRef,
    TaskGoal,
    TaskRevision,
    TaskRevisionType,
    TaskStatus,
)
from .repository import TaskRepository, TaskRepositoryError


class TaskManagerError(RuntimeError):
    """Base error for canonical Task lifecycle operations."""


class TaskNotFoundError(TaskManagerError):
    """The requested Task is not present in the repository."""


class TaskStateTransitionError(TaskManagerError):
    """A caller attempted an invalid deterministic lifecycle transition."""


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
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.PLANNING: {
        TaskStatus.READY,
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.READY: {
        TaskStatus.RUNNING,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PLANNING,
    },
    TaskStatus.RUNNING: {
        TaskStatus.WAITING_HUMAN,
        TaskStatus.WAITING_EXTERNAL,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PLANNING,
    },
    TaskStatus.IN_PROGRESS: {
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
        TaskStatus.CANCELLED,
    },
    # A completed Task is a completed execution of the current GoalTree, not
    # an immutable conversation record.  A later MODIFY turn may revise the
    # same long-lived Task and submit a new Execution.  CANCELLED remains a
    # terminal administrative decision.
    TaskStatus.COMPLETED: {
        TaskStatus.PLANNING,
        TaskStatus.READY,
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

    async def get_active_tasks(
        self,
        conversation_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[Task]:
        tasks = await self._repository.list(conversation_id, statuses=tuple(_ACTIVE_STATUSES))
        return [
            task for task in tasks
            if (user_id is None or task.user_id == user_id)
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
        task.goals = [_task_goal(task.task_id, goal) for goal in goal_tree.all_goals()]
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

    async def append_goal(
        self,
        task_id: str,
        goal: Goal | str,
        *,
        kind: str = "",
        depends_on_goal_ids: Sequence[str] = (),
    ) -> Task:
        task = await self.get_required(task_id)
        description = goal.description if isinstance(goal, Goal) else str(goal)
        description = description.strip()
        if not description:
            raise TaskManagerError("Appended Goal requires a description.")
        goal_id = goal.goal_id if isinstance(goal, Goal) else str(uuid.uuid4())
        task.goals.append(
            TaskGoal(
                goal_id=goal_id,
                task_id=task.task_id,
                description=description,
                kind=kind or (goal.goal_type if isinstance(goal, Goal) else ""),
                depends_on_goal_ids=list(depends_on_goal_ids),
            )
        )
        task.revisions.append(
            TaskRevision(
                task_id=task.task_id,
                type=TaskRevisionType.ADD_GOAL,
                payload={"goal_id": goal_id, "description": description},
                previous_version=task.version,
            )
        )
        if task.status in {TaskStatus.READY, TaskStatus.PAUSED, TaskStatus.FAILED}:
            task.status = self._transition_value(task.status, TaskStatus.PLANNING)
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

    async def resume_task(self, task_id: str) -> Task:
        task = await self.get_required(task_id)
        target = TaskStatus.RUNNING if task.active_execution_id else TaskStatus.READY
        task.status = self._transition_value(task.status, target)
        task.last_action = "RESUME"
        task.last_error = None
        return await self._persist(task)

    async def cancel_task(self, task_id: str, *, reason: str = "") -> Task:
        task = await self._transition(task_id, TaskStatus.CANCELLED)
        task.last_action = "CANCEL"
        if reason:
            task.action_history.append(reason)
        return await self._persist(task)

    async def complete_task(self, task_id: str, *, result: Mapping[str, Any] | None = None) -> Task:
        task = await self._transition(task_id, TaskStatus.COMPLETED)
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
        task.active_execution_id = execution_id
        task.execution_refs.append(
            TaskExecutionRef(
                execution_id=execution_id,
                task_id=task.task_id,
                goal_id=goal_id,
                status=status,
            )
        )
        task.status = self._transition_value(task.status, TaskStatus.RUNNING)
        task.last_action = "BIND_EXECUTION"
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

    async def schedule(self, conversation_id: str) -> Task | None:
        tasks = await self.get_active_tasks(conversation_id)
        candidates = [
            task for task in tasks
            if task.status in {TaskStatus.READY, TaskStatus.CREATED, TaskStatus.PLANNING}
        ]
        return candidates[0] if candidates else None

    async def _transition(self, task_id: str, target: TaskStatus) -> Task:
        task = await self.get_required(task_id)
        task.status = self._transition_value(task.status, target)
        return task

    async def _persist(self, task: Task) -> Task:
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


def _task_goal(task_id: str, goal: Goal) -> TaskGoal:
    return TaskGoal(
        goal_id=goal.goal_id,
        task_id=task_id,
        description=goal.description,
        kind=goal.goal_type,
        depends_on_goal_ids=list(goal.dependencies),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "TaskManager",
    "TaskManagerError",
    "TaskNotFoundError",
    "TaskStateTransitionError",
]
