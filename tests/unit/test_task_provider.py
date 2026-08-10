"""TaskProvider boundary tests without a database or Runtime execution."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

from greenbook_assistant_api.services.task_provider import (
    TaskBinding,
    TaskProvider,
    TaskProviderError,
    TaskScope,
)
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    IntentAction,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.models import Task, TaskIntent, TaskStatus


class _FakeRegistry:
    def __init__(self, tasks: list[Task] | None = None) -> None:
        self.tasks = {task.task_id: task for task in (tasks or [])}

    async def create_task(self, **kwargs: Any) -> Task:
        task = Task(
            task_id=kwargs["task_id"],
            conversation_id=kwargs["conversation_id"],
            user_id=kwargs["user_id"],
            tenant_id=kwargs["tenant_id"],
            goal=kwargs["goal"],
            goal_category=kwargs["goal_category"],
            goal_summary=kwargs.get("goal_summary"),
            status=kwargs["status"],
        )
        self.tasks[task.task_id] = task
        return task

    async def get_task(self, task_id: str) -> Task | None:
        return self.tasks.get(task_id)

    async def list_tasks(self, conversation_id: str) -> list[Task]:
        return [
            task
            for task in self.tasks.values()
            if task.conversation_id == conversation_id
        ]

    async def update_task(self, task_id: str, **fields: Any) -> Task | None:
        task = self.tasks.get(task_id)
        if task is None:
            return None
        updated = task.model_copy(update={**fields, "version": task.version + 1})
        self.tasks[task_id] = updated
        return updated


class _SessionTracker:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    @asynccontextmanager
    async def context(self):
        self.opened += 1
        try:
            yield object()
        finally:
            self.closed += 1


def _provider(
    tasks: list[Task] | None = None,
) -> tuple[TaskProvider, _FakeRegistry, _SessionTracker]:
    registry = _FakeRegistry(tasks)
    tracker = _SessionTracker()
    provider = TaskProvider(
        session_context_factory=tracker.context,
        registry_factory=lambda _session: registry,
    )
    return provider, registry, tracker


def _scope(
    *,
    user_id: str = "user-a",
    tenant_id: str = "tenant-a",
    conversation_id: str = "conversation-1",
) -> TaskScope:
    return TaskScope(
        user_id=user_id,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
    )


def _create_spec(goal: str = "帮我写一篇AI Agent学习路线帖子") -> IntentSpec:
    return IntentSpec(
        mode=IntentMode.SIMPLE,
        goal=goal,
        actions=[
            IntentAction(
                action=ActionType.CREATE,
                resource=ResourceType.CONTENT,
                confidence=0.95,
            ),
        ],
        confidence=0.95,
        source="L1",
    )


def _task(
    task_id: str,
    goal: str,
    *,
    user_id: str = "user-a",
    tenant_id: str = "tenant-a",
    conversation_id: str = "conversation-1",
    category: str = "CREATE_CONTENT",
) -> Task:
    return Task(
        task_id=task_id,
        conversation_id=conversation_id,
        user_id=user_id,
        tenant_id=tenant_id,
        goal=goal,
        goal_category=category,
        status=TaskStatus.COMPLETED,
        updated_at=datetime.now(UTC).isoformat(),
    )


@pytest.mark.asyncio
async def test_create_task_generates_ready_task_in_scope() -> None:
    provider, _registry, tracker = _provider()
    scope = _scope()

    task = await provider.create_task(scope, _create_spec())

    assert task.task_id
    assert task.status == TaskStatus.READY
    assert task.user_id == scope.user_id
    assert task.tenant_id == scope.tenant_id
    assert task.conversation_id == scope.conversation_id
    assert task.goal_category == "CREATE_CONTENT"
    assert tracker.opened == tracker.closed == 1


@pytest.mark.asyncio
async def test_continue_task_resolves_referenced_task() -> None:
    existing = _task("task-java", "写一篇AI Agent学习路线帖子")
    provider, _registry, _tracker = _provider([existing])
    intent = TaskIntent(
        relation="CONTINUE_TASK",
        goal="继续刚才那个帖子",
        goal_category="CREATE_CONTENT",
        target_task_hint="帖子",
    )

    binding = await provider.resolve_task(_scope(), intent)

    assert isinstance(binding, TaskBinding)
    assert binding.task.task_id == existing.task_id
    assert binding.target.task_id == existing.task_id
    assert binding.target.match_level == 2


@pytest.mark.asyncio
async def test_modify_task_resolves_temporal_reference_with_one_candidate() -> None:
    existing = _task("task-java", "写一篇AI Agent学习路线帖子")
    provider, _registry, _tracker = _provider([existing])
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal="把刚才那个改短一点",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="刚才那个",
    )

    binding = await provider.resolve_task(_scope(), intent)

    assert binding.task.task_id == existing.task_id
    assert binding.target.task_id == existing.task_id


@pytest.mark.asyncio
async def test_multiple_matching_tasks_return_ambiguity() -> None:
    tasks = [
        _task("task-java", "写一篇Java学习帖子"),
        _task("task-python", "写一篇Python学习帖子"),
    ]
    provider, _registry, _tracker = _provider(tasks)
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal="把帖子改短一点",
        goal_category="IMPROVE_CONTENT",
        target_task_hint="帖子",
    )

    with pytest.raises(TaskProviderError) as exc_info:
        await provider.resolve_task(_scope(), intent)

    assert exc_info.value.code == "TASK_TARGET_AMBIGUOUS"
    assert set(exc_info.value.candidates) == {"task-java", "task-python"}


@pytest.mark.asyncio
async def test_task_scope_prevents_cross_user_task_access() -> None:
    foreign = _task(
        "task-foreign",
        "用户B的帖子",
        user_id="user-b",
        tenant_id="tenant-b",
    )
    provider, _registry, _tracker = _provider([foreign])
    intent = TaskIntent(
        relation="CONTINUE_TASK",
        goal="继续那个帖子",
        goal_category="CREATE_CONTENT",
        target_task_hint="帖子",
    )

    with pytest.raises(TaskProviderError) as exc_info:
        await provider.resolve_task(_scope(), intent)

    assert exc_info.value.code == "TASK_NOT_FOUND"
    assert exc_info.value.candidates == ()


@pytest.mark.asyncio
async def test_cancel_task_marks_business_task_without_execution_dependency() -> None:
    existing = _task("task-java", "写一篇Java学习帖子")
    provider, registry, _tracker = _provider([existing])
    intent = TaskIntent(
        relation="CANCEL_TASK",
        goal="取消Java帖子",
        goal_category="CREATE_CONTENT",
        target_task_hint="Java",
    )

    cancelled = await provider.cancel_task(_scope(), intent)

    assert cancelled.status == TaskStatus.CANCELLED
    assert registry.tasks[existing.task_id].status == TaskStatus.CANCELLED
