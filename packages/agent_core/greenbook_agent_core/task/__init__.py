"""Canonical durable Task lifecycle subsystem.

Exports stay lazy so TaskManager composition does not create import cycles
with GoalCompiler and the planning contracts.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "InMemoryTaskRepository",
    "Task",
    "TaskManager",
    "TaskConfirmationConflictError",
    "TaskConfirmationTransition",
    "TaskManagerError",
    "TaskNotFoundError",
    "PostgresTaskRepository",
    "TaskRegistryRepository",
    "TaskRepository",
    "TaskRepositoryError",
    "TaskStateTransitionError",
    "TaskStatus",
    "TaskConfirmationState",
    "TaskVersionConflict",
    "TaskVersionConflictError",
]


def __getattr__(name: str) -> Any:
    if name in {
        "TaskManager",
        "TaskConfirmationConflictError",
        "TaskConfirmationTransition",
        "TaskManagerError",
        "TaskNotFoundError",
        "TaskStateTransitionError",
    }:
        from .manager import (
            TaskManager,
            TaskConfirmationConflictError,
            TaskConfirmationTransition,
            TaskManagerError,
            TaskNotFoundError,
            TaskStateTransitionError,
        )

        return {
            "TaskManager": TaskManager,
            "TaskConfirmationConflictError": TaskConfirmationConflictError,
            "TaskConfirmationTransition": TaskConfirmationTransition,
            "TaskManagerError": TaskManagerError,
            "TaskNotFoundError": TaskNotFoundError,
            "TaskStateTransitionError": TaskStateTransitionError,
        }[name]
    if name in {"Task", "TaskStatus", "TaskConfirmationState"}:
        from .models import Task, TaskConfirmationState, TaskStatus

        return {
            "Task": Task,
            "TaskStatus": TaskStatus,
            "TaskConfirmationState": TaskConfirmationState,
        }[name]
    if name in {
        "InMemoryTaskRepository",
        "TaskRegistryRepository",
        "PostgresTaskRepository",
        "TaskRepository",
        "TaskRepositoryError",
        "TaskVersionConflict",
        "TaskVersionConflictError",
    }:
        from .repository import (
            InMemoryTaskRepository,
            PostgresTaskRepository,
            TaskRegistryRepository,
            TaskRepository,
            TaskRepositoryError,
            TaskVersionConflict,
            TaskVersionConflictError,
        )

        return {
            "InMemoryTaskRepository": InMemoryTaskRepository,
            "PostgresTaskRepository": PostgresTaskRepository,
            "TaskRegistryRepository": TaskRegistryRepository,
            "TaskRepository": TaskRepository,
            "TaskRepositoryError": TaskRepositoryError,
            "TaskVersionConflict": TaskVersionConflict,
            "TaskVersionConflictError": TaskVersionConflictError,
        }[name]
    raise AttributeError(name)
