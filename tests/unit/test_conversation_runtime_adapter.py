"""Unit tests for the pre-production ConversationRuntimeAdapter boundary."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_assistant_api.services.task_provider import TaskBinding, TaskScope
from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.task.intent_models import (
    ActionType,
    IntentAction,
    IntentMode,
    IntentSpec,
    ResourceType,
)
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    ResolvedTaskTarget,
    Task,
    TaskStatus,
)


def _spec(
    action: ActionType,
    resource: ResourceType,
    *,
    goal: str = "AI Agent 学习路线",
    target_hint: str | None = None,
) -> IntentSpec:
    return IntentSpec(
        mode=IntentMode.SIMPLE,
        goal=goal,
        actions=[IntentAction(action=action, resource=resource, confidence=0.95)],
        target_hint=target_hint,
        confidence=0.95,
        source="L1",
    )


def _task(
    task_id: str,
    *,
    goal: str = "AI Agent 学习路线",
    artifacts: list[ArtifactRef] | None = None,
) -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal=goal,
        goal_category="CREATE_CONTENT",
        status=TaskStatus.READY,
        artifacts=artifacts or [],
    )


class _IntentProvider:
    def __init__(self, spec: IntentSpec) -> None:
        self.spec = spec
        self.messages: list[str] = []

    async def resolve(self, message: str, *, existing_tasks=None) -> IntentSpec:
        self.messages.append(message)
        return self.spec


class _TaskProvider:
    def __init__(self, task: Task, *, binding: TaskBinding | None = None) -> None:
        self.task = task
        self.binding = binding
        self.created_specs: list[IntentSpec] = []
        self.resolved_scopes: list[TaskScope] = []

    async def list_tasks(self, scope: TaskScope) -> list[Task]:
        return [self.task]

    async def create_task(self, scope: TaskScope, intent_spec: IntentSpec) -> Task:
        self.created_specs.append(intent_spec)
        return self.task

    async def resolve_task(self, scope: TaskScope, intent) -> TaskBinding:
        self.resolved_scopes.append(scope)
        assert self.binding is not None
        return self.binding


class _RuntimeService:
    def __init__(self, result: RuntimeResult) -> None:
        self.result = result
        self.context = None

    async def execute(self, context, **kwargs: Any) -> RuntimeResult:
        self.context = context
        return self.result


class _ExecutionRepository:
    def find_by_id(self, execution_id: str):
        return SimpleNamespace(plan_id="plan-runtime-1")


def _session() -> SessionContext:
    return SessionContext(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
    )


@pytest.mark.asyncio
async def test_create_content_builds_intent_task_and_calls_runtime() -> None:
    spec = _spec(ActionType.CREATE, ResourceType.CONTENT)
    task = _task("task-create")
    intent_provider = _IntentProvider(spec)
    task_provider = _TaskProvider(task)
    runtime = _RuntimeService(
        RuntimeResult(
            success=True,
            status="COMPLETED",
            execution_id="execution-1",
            execution_path="runtime",
        )
    )
    adapter = ConversationRuntimeAdapter(
        intent_provider=intent_provider,
        task_provider=task_provider,
        runtime_service=runtime,
        execution_repository=_ExecutionRepository(),
    )
    session = _session()

    result = await adapter.execute(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        message="帮我写一篇 AI Agent 学习路线帖子",
        session=session,
    )

    assert result.status == "COMPLETED"
    assert result.intent_spec == spec.model_dump(mode="json")
    assert result.task_id == "task-create"
    assert result.plan_id == "plan-runtime-1"
    assert task_provider.created_specs == [spec]
    assert runtime.context is not None
    assert runtime.context.task_context.task_id == "task-create"
    assert session.active_task_id == "task-create"


@pytest.mark.asyncio
async def test_update_existing_task_uses_scoped_task_binding() -> None:
    artifact = ArtifactRef(
        artifact_id="artifact-draft",
        task_id="task-existing",
        artifact_type="DRAFT",
        resource_id="draft-1",
        resource_kind="DRAFT",
    )
    task = _task("task-existing", artifacts=[artifact])
    target = ResolvedTaskTarget(
        task_id=task.task_id,
        match_reason="label_match",
        match_level=2,
    )
    binding = TaskBinding(task=task, target=target)
    task_provider = _TaskProvider(task, binding=binding)
    runtime = _RuntimeService(
        RuntimeResult(success=True, status="COMPLETED", execution_id="execution-2")
    )
    adapter = ConversationRuntimeAdapter(
        intent_provider=_IntentProvider(
            _spec(
                ActionType.UPDATE,
                ResourceType.DRAFT,
                goal="把刚才的帖子改短一点",
                target_hint="刚才的帖子",
            )
        ),
        task_provider=task_provider,
        runtime_service=runtime,
        execution_repository=_ExecutionRepository(),
    )

    result = await adapter.execute(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        message="把刚才的帖子改短一点",
        session=_session(),
    )

    assert result.task_id == task.task_id
    assert task_provider.resolved_scopes == [
        TaskScope(
            user_id="user-1",
            tenant_id="tenant-1",
            conversation_id="conversation-1",
        )
    ]
    assert runtime.context.task_context.target.task_id == task.task_id
    assert not task_provider.created_specs


@pytest.mark.asyncio
async def test_runtime_failure_is_preserved_in_adapter_result() -> None:
    spec = _spec(ActionType.CREATE, ResourceType.CONTENT)
    runtime_failure = RuntimeResult(
        success=False,
        status="FAILED",
        task_id="task-failed",
        execution_id="execution-failed",
        error_code="TOOL_ARGUMENT_VALIDATION_FAILED",
        error_message="title is required",
        execution_path="runtime",
    )
    adapter = ConversationRuntimeAdapter(
        intent_provider=_IntentProvider(spec),
        task_provider=_TaskProvider(_task("task-failed")),
        runtime_service=_RuntimeService(runtime_failure),
    )

    result = await adapter.execute(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        message="写一篇文章",
        session=_session(),
    )

    assert result.success is False
    assert result.status == "FAILED"
    assert result.error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert result.error_message == "title is required"
    assert result.execution_id == "execution-failed"
    assert result.intent_spec == spec.model_dump(mode="json")
