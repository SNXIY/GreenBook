"""Focused Phase 2 coverage for the bounded derived context projection."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from greenbook_agent_core.command.models import (
    Command,
    CommandContext,
    CommandType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import TargetResolutionStatus, TargetResolver
from greenbook_agent_core.context import ContextBuilder, SessionContext
from greenbook_agent_core.context.models import ContextSnapshot
from greenbook_agent_core.context.projection import (
    derive_conversation_context,
    project_interpreter_context,
)
from greenbook_agent_core.execution.action_observation import (
    ActionObservation,
    ActionObservationStore,
)
from greenbook_agent_core.turn import ContextAssembler
from greenbook_agent_api.services.turn_coordinator import TurnCoordinator


def _objective(
    objective_id: str,
    *,
    status: str = "COMPLETED",
    resources: list[str] | None = None,
    target_objective_id: str = "",
    description: str = "消息队列文章",
) -> dict:
    return {
        "objective_id": objective_id,
        "description": description,
        "intent": "CREATE_AND_SCHEDULE",
        "status": status,
        "expected_resource_kind": "DRAFT",
        "related_resource_ids": list(resources or []),
        "constraints": {
            "target_objective_id": target_objective_id,
        },
    }


def _message_queue_snapshot() -> ContextSnapshot:
    return ContextSnapshot(
        conversation_id="conversation-queue",
        user_id="user",
        tenant_id="tenant",
        active_tasks=[{
            "task_id": "task-queue",
            "goal": "写一篇消息队列文章并安排发布",
            "status": "COMPLETED",
            "objectives": [
                _objective(
                    "objective-draft",
                    resources=["draft-queue"],
                    description="消息队列草稿",
                ),
                _objective(
                    "objective-schedule",
                    resources=["schedule-queue"],
                    target_objective_id="objective-draft",
                    description="消息队列定时发布",
                ),
            ],
            "resource_index": [
                {
                    "resource_id": "draft-queue",
                    "resource_kind": "DRAFT",
                    "objective_id": "objective-draft",
                    "title": "消息队列草稿",
                    "status": "DRAFT",
                },
                {
                    "resource_id": "schedule-queue",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-schedule",
                    "title": "消息队列草稿",
                    "status": "SCHEDULED",
                },
            ],
        }],
        recent_verified_outcomes=[{
            "execution_id": "execution-schedule",
            "task_id": "task-queue",
            "goal_id": "objective-schedule",
            "status": "COMPLETED",
            "schedule_id": "schedule-queue",
            "draft_id": "draft-queue",
            "business_result": {
                "schedule": {
                    "schedule_id": "schedule-queue",
                    "draft_id": "draft-queue",
                    "run_at": "2026-08-25T12:00:00Z",
                },
            },
        }],
    )


def test_projection_keeps_owner_for_resolver_but_not_provider() -> None:
    snapshot = _message_queue_snapshot()
    schedule = next(
        item
        for item in snapshot.active_tasks[0]["resource_index"]
        if item["resource_kind"] == "SCHEDULE"
    )
    # This is the projection-loss repair: the Resolver can now see the
    # existing canonical owner without asking the provider to produce it.
    assert schedule["objective_id"] == "objective-schedule"

    provider_view = project_interpreter_context(CommandContext.from_any(snapshot))
    serialized = json.dumps(provider_view, ensure_ascii=False)
    assert "objective-schedule" not in serialized
    assert "schedule-queue" not in serialized
    assert "消息队列" in serialized


def test_original_failure_family_has_one_grounded_schedule_and_lineage() -> None:
    context = derive_conversation_context(
        _message_queue_snapshot(),
        user_input="把这篇消息队列草稿的发布时间改到明天晚上八点。",
    )

    evidence = context.reference_evidence[0]
    assert evidence["candidate_cardinality"] == 1
    assert evidence["matched_resource_kinds"] == ["DRAFT"]

    schedule = next(
        item
        for item in context.relevant_resources
        if item["resource_kind"] == "SCHEDULE"
    )
    assert schedule["owner_objective_id"] == "objective-schedule"
    assert "objective-draft" in schedule["objective_lineage"]
    assert any(
        item["resource_id"] == "draft-queue"
        and "ActionObservation.business_result" in item["evidence_sources"]
        for item in schedule["related_resources"]
    )
    assert schedule["verified_outcome"]["status"] == "COMPLETED"


def test_two_java_resources_preserve_ambiguity_and_terminal_is_not_current() -> None:
    snapshot = ContextSnapshot(
        conversation_id="conversation-java",
        active_tasks=[{
            "task_id": "task-java",
            "goal": "Java 文章",
            "status": "COMPLETED",
            "objectives": [
                _objective("objective-java-one", resources=["schedule-java-one"], description="Java 第一篇"),
                _objective("objective-java-two", resources=["schedule-java-two"], description="Java 第二篇"),
            ],
            "resource_index": [
                {
                    "resource_id": "schedule-java-one",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-java-one",
                    "title": "Java 文章",
                    "status": "SCHEDULED",
                },
                {
                    "resource_id": "schedule-java-two",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-java-two",
                    "title": "Java 文章",
                    "status": "SCHEDULED",
                },
                {
                    "resource_id": "schedule-java-old",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-java-one",
                    "title": "Java 文章",
                    "status": "PUBLISHED",
                },
            ],
        }],
    )
    context = derive_conversation_context(
        snapshot,
        user_input="Java 那篇改到明天下午五点发。",
    )
    assert context.reference_evidence[0]["candidate_cardinality"] == 2
    assert {
        item["resource_id"]
        for item in context.relevant_resources
        if item["lifecycle"] != "TERMINAL"
    } == {"schedule-java-one", "schedule-java-two"}
    old = next(item for item in context.relevant_resources if item["resource_id"] == "schedule-java-old")
    assert old["lifecycle"] == "TERMINAL"
    assert old["context_tier"] == "COLD"


def _two_draft_delta_candidates() -> list[dict]:
    return [
        {
            "id": "objective-java",
            "goal_id": "objective-java",
            "objective_id": "objective-java",
            "task_id": "task-java",
            "kind": "TASK",
            "label": "Java 帖子",
            "resource_index": [{
                "resource_id": "draft-java",
                "resource_kind": "DRAFT",
                "objective_id": "objective-java",
                "label": "Java 帖子",
            }],
        },
        {
            "id": "objective-redis",
            "goal_id": "objective-redis",
            "objective_id": "objective-redis",
            "task_id": "task-redis",
            "kind": "TASK",
            "label": "Redis 帖子",
            "resource_index": [{
                "resource_id": "draft-redis",
                "resource_kind": "DRAFT",
                "objective_id": "objective-redis",
                "label": "Redis 帖子",
            }],
        },
    ]


def _provider_label_delta(label: str) -> TaskDelta:
    return TaskDelta(
        operation=TaskDeltaOperation.UPDATE_GOAL,
        target_reference={"kind": "TASK", "label": label},
        desired_changes={"semantic_action": "UPDATE_DRAFT"},
    )


def test_unsupported_provider_label_preserves_anaphoric_ambiguity() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _provider_label_delta("Java 帖子"),
        _two_draft_delta_candidates(),
        user_input="把那篇改改。",
    )
    assert resolution.is_ambiguous
    assert len(resolution.candidates) == 2


def test_provider_label_with_current_turn_topic_remains_resolvable() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _provider_label_delta("Java 帖子"),
        _two_draft_delta_candidates(),
        user_input="Java 那篇改改。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.task_id == "task-java"


def test_long_provider_label_uses_unique_natural_cjk_subject() -> None:
    candidates = [
        {
            "id": "objective-queue",
            "goal_id": "objective-queue",
            "objective_id": "objective-queue",
            "task_id": "task-queue",
            "kind": "TASK",
            "label": "消息队列可靠性实践",
            "resource_index": [{
                "resource_id": "draft-queue",
                "resource_kind": "DRAFT",
                "objective_id": "objective-queue",
                "label": "更简洁的标题",
            }],
        },
        {
            "id": "objective-java",
            "goal_id": "objective-java",
            "objective_id": "objective-java",
            "task_id": "task-java",
            "kind": "TASK",
            "label": "Java 后端稳定性实践",
            "resource_index": [{
                "resource_id": "draft-java",
                "resource_kind": "DRAFT",
                "objective_id": "objective-java",
                "label": "Java 后端稳定性实践",
            }],
        },
    ]
    resolution = TargetResolver().resolve_task_delta(
        _provider_label_delta("消息队列可靠性实践"),
        candidates,
        user_input="给刚才修改标题的消息队列草稿补充一段说明，仍然只保存草稿。",
    )
    assert resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.task_id == "task-queue"


def test_same_subject_keeps_multiple_provider_targets_ambiguous() -> None:
    candidates = [
        {
            "id": "objective-java-a",
            "goal_id": "objective-java-a",
            "objective_id": "objective-java-a",
            "task_id": "task-java-a",
            "kind": "TASK",
            "label": "Java backend stability Alpha",
            "resource_index": [{"resource_id": "draft-java-a", "resource_kind": "DRAFT", "label": "Java backend stability Alpha"}],
        },
        {
            "id": "objective-java-b",
            "goal_id": "objective-java-b",
            "objective_id": "objective-java-b",
            "task_id": "task-java-b",
            "kind": "TASK",
            "label": "Java backend stability Beta",
            "resource_index": [{"resource_id": "draft-java-b", "resource_kind": "DRAFT", "label": "Java backend stability Beta"}],
        },
    ]
    resolution = TargetResolver().resolve_task_delta(
        _provider_label_delta("Java backend stability Alpha"),
        candidates,
        user_input="Please add another paragraph to the Java draft.",
    )
    assert resolution.is_ambiguous
    assert {item.task_id for item in resolution.candidates} == {"task-java-a", "task-java-b"}


def test_explicit_unknown_topic_remains_not_found() -> None:
    resolution = TargetResolver().resolve_task_delta(
        _provider_label_delta("Redis post"),
        _two_draft_delta_candidates()[:1],
        user_input="Redis post update",
    )
    assert resolution.status == TargetResolutionStatus.NOT_FOUND


def test_completed_create_objective_does_not_make_draft_historical() -> None:
    snapshot = ContextSnapshot(
        conversation_id="conversation-redis",
        active_tasks=[{
            "task_id": "task-redis",
            "status": "COMPLETED",
            "objectives": [
                _objective(
                    "objective-redis",
                    resources=["draft-redis"],
                    description="Redis \u7f13\u5b58\u5b9e\u8df5",
                ),
            ],
            "resource_index": [{
                "resource_id": "draft-redis",
                "resource_kind": "DRAFT",
                "objective_id": "objective-redis",
                "title": "Redis \u7f13\u5b58\u5b9e\u8df5",
                "status": "",
            }],
        }],
    )
    context = derive_conversation_context(
        snapshot,
        user_input="\u7ed9\u521a\u624d\u90a3\u7bc7 Redis \u8349\u7a3f\u8865\u5145\u4e00\u6bb5\u5185\u5bb9\u3002",
    )
    assert context.reference_evidence[0]["candidate_cardinality"] == 1
    draft = context.relevant_resources[0]
    assert draft["resource_kind"] == "DRAFT"
    assert draft["lifecycle"] == "CURRENT"


def test_normal_create_is_not_reinterpreted_as_a_reference() -> None:
    context = derive_conversation_context(
        _message_queue_snapshot(),
        user_input="写一篇 Java 草稿，只保存不发布。",
    )
    assert context.reference_evidence == []
    assert {
        item["resource_id"] for item in context.relevant_resources
    } == {"draft-queue", "schedule-queue"}


def test_action_constraint_is_not_a_missing_resource_reference() -> None:
    context = derive_conversation_context(
        _message_queue_snapshot(),
        user_input="\u628a Java \u5b89\u6392\u5230\u660e\u5929\u4e0a\u5348\u4e5d\u70b9\uff0cAgent \u53ea\u4fdd\u7559\u8349\u7a3f\u3002",
    )
    assert context.reference_evidence == []
    assert {
        item["resource_id"] for item in context.relevant_resources
    } == {"draft-queue", "schedule-queue"}


def test_proximal_schedule_reference_uses_typed_kind_without_latest_fallback() -> None:
    context = derive_conversation_context(
        _single_schedule_snapshot("Java", "task-java"),
        user_input="\u521a\u521a\u5b9a\u65f6\u7684\u90a3\u7bc7\u6539\u5230\u660e\u5929\u4e0b\u5348\u3002",
    )
    assert context.reference_evidence[0]["candidate_cardinality"] == 1
    assert context.reference_evidence[0]["matched_resource_kinds"] == ["SCHEDULE"]
    assert [item["resource_kind"] for item in context.relevant_resources] == [
        "SCHEDULE"
    ]


def test_multi_objective_reference_keeps_each_mentioned_task_package() -> None:
    tasks = []
    for topic in ("Java", "Agent", "Redis"):
        task_id = f"task-{topic.casefold()}"
        objective_id = f"objective-{topic.casefold()}"
        tasks.append({
            "task_id": task_id,
            "goal": f"{topic} \u6587\u7ae0",
            "status": "PENDING",
            "objectives": [_objective(
                objective_id,
                status="PENDING",
                resources=[f"draft-{topic.casefold()}", f"schedule-{topic.casefold()}"],
                description=f"{topic} \u6587\u7ae0",
            )],
            "resource_index": [
                {
                    "resource_id": f"draft-{topic.casefold()}",
                    "resource_kind": "DRAFT",
                    "objective_id": objective_id,
                    "title": f"{topic} \u6587\u7ae0",
                    "status": "DRAFT",
                },
                {
                    "resource_id": f"schedule-{topic.casefold()}",
                    "resource_kind": "SCHEDULE",
                    "objective_id": objective_id,
                    "title": f"{topic} \u6587\u7ae0",
                    "status": "SCHEDULED",
                },
            ],
        })
    context = derive_conversation_context(
        ContextSnapshot(active_tasks=tasks),
        user_input="Java \u90a3\u7bc7\u76f4\u63a5\u53d1，Agent \u90a3\u7bc7\u5148\u522b\u52a8，再给我写篇 Redis 草稿。",
    )
    assert {
        item["task_id"] for item in context.relevant_resources
    } == {"task-java", "task-agent", "task-redis"}


@pytest.mark.asyncio
async def test_assembled_context_is_the_same_scope_for_provider_and_resolver() -> None:
    class Builder:
        async def build(self, **_kwargs):
            return _message_queue_snapshot()

    assembled = await ContextAssembler(Builder()).assemble(
        conversation_id="conversation-queue",
        user_id="user",
        tenant_id="tenant",
        user_input="把这篇消息队列草稿的发布时间改到明天晚上八点。",
    )
    command_context = assembled.to_command_context()
    task_ids = {
        item.get("task_id")
        for item in command_context.active_tasks
    }
    assert task_ids == {"task-queue"}
    assert {
        item.get("resource_id")
        for item in command_context.targets
        if item.get("resource_kind")
    } == {"draft-queue", "schedule-queue"}
    provider = project_interpreter_context(command_context)
    assert "schedule-queue" not in json.dumps(provider, ensure_ascii=False)
    assert provider["reference_evidence"][0]["candidate_cardinality"] == 1


@pytest.mark.asyncio
async def test_context_reads_bounded_action_observation_receipts() -> None:
    store = ActionObservationStore()
    store.save(ActionObservation(
        execution_id="execution-relevant",
        task_id="task-queue",
        goal_id="objective-schedule",
        capability="SCHEDULE_PUBLISH",
        status="COMPLETED",
        schedule_id="schedule-queue",
        draft_id="draft-queue",
        payload={"large_internal_resume_tree": "not part of context"},
    ))
    store.save(ActionObservation(
        execution_id="execution-unrelated",
        task_id="another-task",
        status="FAILED",
    ))

    class Tasks:
        async def list_tasks(self, _scope):
            task = SimpleNamespace(
                task_id="task-queue",
                conversation_id="conversation-queue",
                user_id="user",
                tenant_id="tenant",
                goal="消息队列文章",
                status="COMPLETED",
                objectives=[],
                goals=[],
                artifacts=[],
                execution_refs=[],
                resource_index=[],
            )
            return [task]

    snapshot = await ContextBuilder(
        task_provider=Tasks(),
        observation_store=store,
    ).build(
        conversation_id="conversation-queue",
        user_id="user",
        tenant_id="tenant",
        session=SessionContext(
            conversation_id="conversation-queue",
            user_id="user",
            tenant_id="tenant",
        ),
    )
    assert [item["execution_id"] for item in snapshot.recent_verified_outcomes] == [
        "execution-relevant"
    ]
    assert "large_internal_resume_tree" not in json.dumps(
        snapshot.recent_verified_outcomes,
        ensure_ascii=False,
    )


async def _resolve_schedule_delta(snapshot: ContextSnapshot, text: str, label: str):
    class Builder:
        async def build(self, **_kwargs):
            return snapshot

    assembled = await ContextAssembler(Builder()).assemble(
        conversation_id=snapshot.conversation_id,
        user_id="user",
        tenant_id="tenant",
        user_input=text,
    )
    coordinator = object.__new__(TurnCoordinator)
    coordinator._target_resolver = TargetResolver()
    command = Command(
        type=CommandType.MODIFY,
        raw_input=text,
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "SCHEDULE", "label": label},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-25T12:00:00Z",
            },
        )],
    )
    return await _resolve_delta(coordinator, command, assembled)


async def _resolve_delta(coordinator: TurnCoordinator, command: Command, assembled):
    return coordinator._resolve_delta_objective_target(command, assembled)


def _single_schedule_snapshot(topic: str, task_id: str) -> ContextSnapshot:
    objective_id = f"objective-{topic.casefold()}"
    schedule_id = f"schedule-{topic.casefold()}"
    return ContextSnapshot(
        conversation_id=f"conversation-{topic.casefold()}",
        active_tasks=[{
            "task_id": task_id,
            "goal": f"写一篇 {topic} 文章并安排发布",
            "status": "COMPLETED",
            "objectives": [_objective(
                objective_id,
                resources=[schedule_id],
                description=f"{topic} 定时发布",
            )],
            "resource_index": [{
                "resource_id": schedule_id,
                "resource_kind": "SCHEDULE",
                "objective_id": objective_id,
                "title": f"{topic} 草稿",
                "status": "SCHEDULED",
            }],
        }],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "topic"),
    [
        ("Java 那篇改到明天下午五点发。", "Java"),
        ("刚才安排发布的 Agent 草稿改成晚上八点。", "Agent"),
        ("Redis 那篇定时改晚两个小时。", "Redis"),
    ],
)
async def test_failure_family_natural_variants_resolve_one_owner(text: str, topic: str) -> None:
    snapshot = _single_schedule_snapshot(topic, f"task-{topic.casefold()}")
    resolution = await _resolve_schedule_delta(snapshot, text, f"{topic} 草稿")
    assert resolution is not None and resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.task_id == f"task-{topic.casefold()}"


@pytest.mark.asyncio
async def test_draft_reference_for_schedule_mutation_resolves_schedule_target() -> None:
    """A Draft label may identify the Schedule lineage, not the write id."""

    snapshot = ContextSnapshot(
        conversation_id="conversation-queue-schedule-reference",
        active_tasks=[{
            "task_id": "task-queue-schedule-reference",
            "goal": "\u6d88\u606f\u961f\u5217\u53ef\u9760\u6027\u5b9e\u8df5",
            "status": "COMPLETED",
            "objectives": [
                _objective(
                    "objective-queue-schedule",
                    resources=["schedule-queue-schedule-reference"],
                    description="\u6d88\u606f\u961f\u5217\u53ef\u9760\u6027\u5b9e\u8df5",
                ),
            ],
            "resource_index": [{
                "resource_id": "schedule-queue-schedule-reference",
                "resource_kind": "SCHEDULE",
                "objective_id": "objective-queue-schedule",
                "status": "SCHEDULED",
            }],
        }],
    )
    assembled = SimpleNamespace(snapshot=snapshot)
    command = Command(
        type=CommandType.MODIFY,
        raw_input="\u628a\u8fd9\u7bc7\u6d88\u606f\u961f\u5217\u8349\u7a3f\u7684\u53d1\u5e03\u65f6\u95f4\u6539\u5230\u660e\u5929\u665a\u4e0a\u516b\u70b9\u3002",
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "DRAFT",
                "label": "\u6d88\u606f\u961f\u5217\u53ef\u9760\u6027\u5b9e\u8df5",
            },
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-25T12:00:00Z",
            },
        )],
    )

    resolution = TurnCoordinator(
        target_resolver=TargetResolver(),
    )._resolve_delta_objective_target(command, assembled)

    assert resolution is not None and resolution.is_resolved
    assert resolution.target is not None
    assert resolution.target.resource_id == "schedule-queue-schedule-reference"


@pytest.mark.asyncio
async def test_two_java_schedules_require_clarification() -> None:
    snapshot = ContextSnapshot(
        conversation_id="conversation-java-peers",
        active_tasks=[{
            "task_id": "task-java-peers",
            "goal": "Java 文章",
            "status": "COMPLETED",
            "objectives": [
                _objective("objective-java-a", resources=["schedule-java-a"], description="Java 草稿 A"),
                _objective("objective-java-b", resources=["schedule-java-b"], description="Java 草稿 B"),
            ],
            "resource_index": [
                {
                    "resource_id": "schedule-java-a",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-java-a",
                    "title": "Java 草稿",
                    "status": "SCHEDULED",
                },
                {
                    "resource_id": "schedule-java-b",
                    "resource_kind": "SCHEDULE",
                    "objective_id": "objective-java-b",
                    "title": "Java 草稿",
                    "status": "SCHEDULED",
                },
            ],
        }],
    )
    resolution = await _resolve_schedule_delta(
        snapshot,
        "Java 那篇改时间。",
        "Java 草稿",
    )
    assert resolution is not None
    assert resolution.is_ambiguous
    assert resolution.candidates


@pytest.mark.asyncio
async def test_legacy_resource_without_owner_fails_closed() -> None:
    snapshot = ContextSnapshot(
        conversation_id="conversation-legacy-owner",
        active_tasks=[{
            "task_id": "task-legacy",
            "goal": "Java 文章",
            "status": "COMPLETED",
            "objectives": [_objective(
                "objective-legacy",
                resources=[],
                description="Java 定时发布",
            )],
            "resource_index": [{
                "resource_id": "schedule-legacy",
                "resource_kind": "SCHEDULE",
                "title": "Java 草稿",
                "status": "SCHEDULED",
            }],
        }],
    )
    resolution = await _resolve_schedule_delta(
        snapshot,
        "Java 那篇改到明天下午五点发。",
        "Java 草稿",
    )
    assert resolution is not None
    assert not resolution.is_resolved
