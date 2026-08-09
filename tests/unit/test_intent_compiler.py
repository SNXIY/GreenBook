"""Tests for the Assistant semantic IntentCompiler boundary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_assistant_api.models.runtime_context import TargetContext, TaskContext
from greenbook_assistant_api.services.intent_compiler import (
    IntentCompilationError,
    IntentCompiler,
)
from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    ResolvedTaskTarget,
    Task,
    TaskIntent,
)


def _task(task_id: str, *, artifacts: list[ArtifactRef] | None = None) -> Task:
    return Task(
        task_id=task_id,
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal="内容创作任务",
        artifacts=artifacts or [],
    )


def _draft(task_id: str, artifact_id: str = "draft-artifact-1") -> ArtifactRef:
    return ArtifactRef(
        artifact_id=artifact_id,
        task_id=task_id,
        artifact_type="DRAFT",
        resource_id="draft-1",
        resource_kind="DRAFT",
    )


def test_compile_new_task_context() -> None:
    task = _task("task-1")
    intent = TaskIntent(
        relation="NEW_TASK",
        goal="AI Agent 学习路线内容创作",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
        resource_requests=[
            {"operation": "CREATE", "resource_type": "CONTENT_DRAFT"}
        ],
    )

    context = IntentCompiler().compile(
        task_intent=intent,
        task=task,
        conversation=SimpleNamespace(
            active_task_id=None,
            active_artifact_id=None,
        ),
        artifacts=(),
    )

    assert isinstance(context, TaskContext)
    assert context.task_id == "task-1"
    assert context.task_intent.relation == "NEW_TASK"
    assert context.task_intent.requirements == [{"type": "CREATE"}]
    assert context.artifact_refs == ()


def test_compile_revision_binds_same_task_and_draft() -> None:
    draft = _draft("task-1")
    task = _task("task-1", artifacts=[draft])
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal="增加代码逻辑",
        goal_category="IMPROVE_CONTENT",
        requirements=[
            {"type": "CONTENT_REVISE", "change": "ADD_CODE_LOGIC"}
        ],
        resource_requests=[
            {"operation": "UPDATE", "resource_type": "CONTENT_DRAFT"}
        ],
    )

    context = IntentCompiler().compile(
        task_intent=intent,
        target_context=TargetContext(
            task_id="task-1",
            artifact_id=draft.artifact_id,
            resource_id=draft.resource_id,
            resource_kind=draft.resource_kind,
        ),
        task=task,
        conversation=SimpleNamespace(
            active_task_id="task-1",
            active_artifact_id=draft.artifact_id,
        ),
        artifacts=task.artifacts,
    )

    assert context.task_id == "task-1"
    assert context.target is not None
    assert context.target.artifact_id == draft.artifact_id
    assert context.target.resource_id == "draft-1"
    assert context.task_intent.requirements == [
        {"type": "CONTENT_REVISE", "change": "ADD_CODE_LOGIC"}
    ]


def test_compile_publish_binds_existing_draft() -> None:
    draft = _draft("task-1")
    task = _task("task-1", artifacts=[draft])
    intent = TaskIntent(
        relation="CONTINUE_TASK",
        goal="五分钟之后发布",
        goal_category="PUBLISH_CONTENT",
        requirements=[{"type": "PUBLISH"}],
        constraints=[{"type": "TIME", "value": "五分钟之后"}],
        resource_requests=[
            {"operation": "CREATE", "resource_type": "SCHEDULE"}
        ],
    )

    context = IntentCompiler().compile(
        task_intent=intent,
        target_context=TargetContext(
            task_id="task-1",
            artifact_id=draft.artifact_id,
            resource_id=draft.resource_id,
            resource_kind=draft.resource_kind,
        ),
        task=task,
        conversation=SimpleNamespace(
            active_task_id="task-1",
            active_artifact_id=draft.artifact_id,
        ),
        artifacts=task.artifacts,
        timezone="Asia/Shanghai",
    )

    assert context.task_id == "task-1"
    assert context.target.resource_id == "draft-1"
    assert context.task_intent.goal_category == "PUBLISH_CONTENT"
    assert context.constraints == (
        {"type": "TIME", "value": "五分钟之后"},
    )


def test_compile_rejects_ambiguous_target() -> None:
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal="修改内容",
        goal_category="IMPROVE_CONTENT",
        requirements=[{"type": "CONTENT_REVISE"}],
    )

    with pytest.raises(IntentCompilationError) as exc_info:
        IntentCompiler().compile(
            task_intent=intent,
            target_context=ResolvedTaskTarget(
                task_id="",
                candidates=["task-a", "task-b"],
                is_ambiguous=True,
            ),
            conversation=SimpleNamespace(
                active_task_id=None,
                active_artifact_id=None,
            ),
            artifacts=(),
        )

    assert exc_info.value.code == "AMBIGUOUS_TARGET"
    assert exc_info.value.candidates == ("task-a", "task-b")


def test_artifact_must_belong_to_task() -> None:
    intent = TaskIntent(
        relation="MODIFY_TASK",
        goal="修改内容",
        goal_category="IMPROVE_CONTENT",
        requirements=[{"type": "CONTENT_REVISE"}],
    )
    foreign_artifact = _draft("task-2")

    with pytest.raises(IntentCompilationError) as exc_info:
        IntentCompiler().compile(
            task_intent=intent,
            target_context=TargetContext(
                task_id="task-1",
                artifact_id=foreign_artifact.artifact_id,
                resource_id=foreign_artifact.resource_id,
                resource_kind=foreign_artifact.resource_kind,
            ),
            task=_task("task-1"),
            conversation=SimpleNamespace(
                active_task_id="task-1",
                active_artifact_id=foreign_artifact.artifact_id,
            ),
            artifacts=[foreign_artifact],
        )

    assert exc_info.value.code == "ARTIFACT_TASK_MISMATCH"


def test_reloaded_conversation_keeps_task_and_artifact_binding() -> None:
    draft = _draft("task-1")
    task = _task("task-1", artifacts=[draft])
    created = SessionContext(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        active_task_id="task-1",
        active_artifact_id=draft.artifact_id,
    )
    reloaded = SessionContext.model_validate(created.model_dump())

    context = IntentCompiler().compile(
        task_intent=TaskIntent(
            relation="MODIFY_TASK",
            goal="修改内容",
            goal_category="IMPROVE_CONTENT",
            requirements=[{"type": "CONTENT_REVISE"}],
        ),
        task=task,
        conversation=reloaded,
        artifacts=task.artifacts,
    )

    assert context.task_id == "task-1"
    assert context.target is not None
    assert context.target.artifact_id == draft.artifact_id


def test_runtime_service_has_no_semantic_understanding_dependency() -> None:
    from greenbook_assistant_api.services import runtime_agent_service

    assert not hasattr(runtime_agent_service, "TaskUnderstanding")
    assert not hasattr(runtime_agent_service, "TargetResolver")
