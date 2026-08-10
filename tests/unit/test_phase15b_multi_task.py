"""Phase15-B multi-task, multi-goal, and cross-turn regression tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from greenbook_assistant_api.models.runtime_result import RuntimeResult
from greenbook_assistant_api.services.conversation_runtime_adapter import ConversationRuntimeAdapter
from greenbook_assistant_api.services.task_provider import TaskBinding, TaskScope
from greenbook_assistant_core.orchestration.orchestrator import TaskOrchestrator
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
    TaskResourceRef,
    TaskStatus,
)
from greenbook_assistant_core.task.multi_task import (
    ConversationTargetResolver,
    ConversationTaskIndex,
    IntentDelta,
    apply_intent_delta,
    parse_ordinal,
    split_task_segments,
)


def _task(task_id: str, goal: str, *, created: str, action: str | None = None) -> Task:
    task = Task(
        task_id=task_id,
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        goal=goal,
        goal_summary=goal,
        goal_category="CREATE_CONTENT",
        status=TaskStatus.READY,
        created_at=created,
        updated_at=created,
        last_action=action,
    )
    task.resource_index = [
        TaskResourceRef(
            resource_id=f"draft-{task_id}",
            resource_kind="DRAFT",
            title=goal,
        ),
        TaskResourceRef(
            resource_id=f"schedule-{task_id}",
            resource_kind="SCHEDULE",
            status="ACTIVE",
        ),
    ]
    return task


def test_explicit_multi_task_split_and_query_split() -> None:
    message = (
        "帮我做两件事：第一，写一篇Java文章，明天发布。"
        "第二，单独写一篇Redis文章，后天发布。"
    )
    segments = split_task_segments(message)
    assert [segment.text for segment in segments] == [
        "写一篇Java文章，明天发布",
        "单独写一篇Redis文章，后天发布",
    ]

    mixed = "Java那篇取消发布；Redis那篇保持原计划。然后分析热门帖子，只告诉我结论。"
    mixed_segments = split_task_segments(mixed)
    assert len(mixed_segments) == 3
    assert mixed_segments[-1].is_query is True


def test_structured_target_resolution_does_not_guess() -> None:
    now = datetime.now(UTC)
    tasks = [
        _task("task-java", "Java 后端实习准备", created=now.isoformat(), action="UPDATE_TITLE"),
        _task("task-redis", "Redis 缓存三大问题", created=(now + timedelta(seconds=1)).isoformat()),
    ]
    resolver = ConversationTargetResolver()

    assert resolver.resolve("取消第二个", tasks).task.task_id == "task-redis"
    assert resolver.resolve("刚才改过标题的那篇", tasks).task.task_id == "task-java"
    ambiguous = resolver.resolve("把刚才那个改一下", tasks)
    assert ambiguous.is_ambiguous is True
    assert {task.task_id for task in ambiguous.candidates} == {"task-java", "task-redis"}
    assert parse_ordinal("第二个任务") == 2


def test_cancelled_schedule_reference_and_delta_keep_task_identity() -> None:
    task = _task("task-redis", "Redis 缓存三大问题", created="2026-08-10T12:00:00+00:00")
    apply_intent_delta(task, IntentDelta.from_message("取消发布", target_task_ids=[task.task_id]))
    assert task.task_id == "task-redis"
    assert task.resource_index[1].status == "CANCELLED"
    assert task.last_action == "CANCEL_SCHEDULE"
    target = ConversationTargetResolver().resolve("那篇取消发布的文章", [task])
    assert target.task is task


def test_eight_round_state_regression_preserves_two_tasks_and_read_only_query() -> None:
    now = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    java = _task("task-java", "Java 后端实习怎么准备", created=now.isoformat())
    redis = _task("task-redis", "Redis缓存三大问题：穿透、击穿、雪崩", created=(now + timedelta(seconds=1)).isoformat())
    index = ConversationTaskIndex([java, redis])

    # Round 2: Java title/schedule only; Redis is untouched.
    apply_intent_delta(java, IntentDelta.from_message(
        "标题改成《Java后端实习准备指南：从八股到项目实战》，明天下午2点发布",
        target_task_ids=[java.task_id],
    ))
    index.mark_action(java.task_id, "UPDATE_TITLE")
    index.mark_action(java.task_id, "UPDATE_SCHEDULE")

    # Round 3: Redis content change, schedule remains active.
    index.mark_action(redis.task_id, "UPDATE_CONTENT")

    # Round 4: historical title action resolves Java, even though the latest
    # action on Java was a schedule change.
    target = index.resolve("刚才改过标题的那篇")
    assert target.task is java
    apply_intent_delta(java, IntentDelta.from_message("再提前半小时", target_task_ids=[java.task_id]))

    # Round 5 is query-only and therefore changes no Task state.
    before_query = [(task.task_id, task.last_action, task.updated_at) for task in index.list()]
    assert len(index.list()) == 2
    assert before_query == [(task.task_id, task.last_action, task.updated_at) for task in index.list()]

    # Round 6/7: cancel Redis schedule, retain draft, then resolve the
    # cancelled schedule and republish it under the new title/time.
    apply_intent_delta(redis, IntentDelta.from_message("取消定时发布", target_task_ids=[redis.task_id]))
    cancelled = index.resolve("那篇取消发布的文章")
    assert cancelled.task is redis
    apply_intent_delta(redis, IntentDelta.from_message(
        "标题改成《Redis缓存风险》，下周一上午9点重新发布",
        target_task_ids=[redis.task_id],
    ))
    assert redis.resource_index[0].title == "Redis缓存风险"
    assert redis.resource_index[1].status == "ACTIVE"

    # Round 8: Java cancellation plus a read-only analysis must not affect
    # Redis or create a third Task.
    apply_intent_delta(java, IntentDelta.from_message("取消发布", target_task_ids=[java.task_id]))
    assert java.resource_index[1].status == "CANCELLED"
    assert redis.resource_index[1].status == "ACTIVE"
    assert len(index.list()) == 2


def test_multi_goal_plan_uses_existing_dag_and_artifact_handoffs() -> None:
    plan = TaskOrchestrator().generate_goal_plan(
        task_id="task-java",
        goals=[
            {"kind": "SEARCH", "description": "查询热门帖子"},
            {"kind": "ANALYZE", "description": "总结共同特征"},
            {"kind": "GENERATE", "description": "生成文章"},
            {"kind": "DRAFT", "description": "创建草稿"},
            {"kind": "PUBLISH", "description": "定时发布"},
        ],
        requirements=[
            {"type": "SEARCH"}, {"type": "ANALYZE"},
            {"type": "CREATE"}, {"type": "PUBLISH"},
        ],
    )
    assert [step.capability for step in plan.plan.steps] == [
        "SEARCH_COMMUNITY", "ANALYZE_CONTENT_PATTERNS",
        "GENERATE_CONTENT", "VALIDATE_QUALITY", "SCHEDULE_PUBLISH",
    ]
    assert plan.plan.steps[1].input_artifact_types == ["SEARCH_RESULT"]
    assert plan.plan.steps[2].input_artifact_types == ["ANALYSIS_REPORT"]
    assert plan.plan.steps[-1].input_artifact_types == ["DRAFT"]
    assert plan.plan.steps[0].goal_id == plan.goals[0].goal_id
    assert plan.plan.steps[-1].goal_id == plan.goals[4].goal_id


class _IntentProvider:
    async def resolve(self, message: str, *, existing_tasks=None) -> IntentSpec:
        if "分析热门" in message:
            return IntentSpec(
                mode=IntentMode.SIMPLE,
                goal="分析最近Java热门帖子",
                actions=[IntentAction(action=ActionType.QUERY, resource=ResourceType.POST)],
            )
        resource = ResourceType.CONTENT
        return IntentSpec(
            mode=IntentMode.SIMPLE,
            goal="Redis文章" if "Redis" in message else "Java文章",
            actions=[IntentAction(action=ActionType.CREATE, resource=resource)],
        )


class _TaskProvider:
    def __init__(self) -> None:
        self.tasks: list[Task] = []

    async def list_tasks(self, scope: TaskScope) -> list[Task]:
        return list(self.tasks)

    async def create_task(self, scope: TaskScope, intent_spec: IntentSpec) -> Task:
        task = _task(
            f"task-{len(self.tasks) + 1}", intent_spec.goal,
            created=f"2026-08-10T12:0{len(self.tasks)}:00+00:00",
        )
        self.tasks.append(task)
        return task

    async def resolve_task(self, scope: TaskScope, intent) -> TaskBinding:
        task = self.tasks[0]
        return TaskBinding(task, ResolvedTaskTarget(task_id=task.task_id))


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, context: Any, **kwargs: Any) -> RuntimeResult:
        self.calls.append(context.task_id)
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            task_id=context.task_id,
            execution_id=f"execution-{context.task_id}",
            content=f"done:{context.task_id}",
        )


@pytest.mark.asyncio
async def test_adapter_dispatches_two_tasks_and_query_without_execution() -> None:
    provider = _TaskProvider()
    runtime = _Runtime()
    adapter = ConversationRuntimeAdapter(
        intent_provider=_IntentProvider(), task_provider=provider, runtime_service=runtime,
    )
    result = await adapter.execute(
        conversation_id="conversation-1", user_id="user-1", tenant_id="tenant-1",
        message=(
            "帮我做两件事：第一，写一篇Java文章。"
            "第二，单独写一篇Redis文章。"
        ),
    )
    assert result.status == "COMPLETED"
    assert result.partial_results["multi_task"] is True
    assert len(result.partial_results["task_ids"]) == 2
    assert len(result.partial_results["execution_ids"]) == 2
    assert len(runtime.calls) == 2

    query = await adapter.execute(
        conversation_id="conversation-1", user_id="user-1", tenant_id="tenant-1",
        message="然后分析热门帖子，只告诉我结论。",
    )
    assert query.success is True
    assert query.partial_results == {"query_only": True, "side_effect": False}
    assert len(provider.tasks) == 2
    assert len(runtime.calls) == 2
