from __future__ import annotations

from datetime import datetime

from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from greenbook_agent_core.command.models import (
    Command,
    CommandItem,
    CommandType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.execution.temporal_resolver import TemporalResolver


def _coordinator() -> TurnCoordinator:
    return TurnCoordinator(
        temporal_resolver=TemporalResolver(
            now=datetime.fromisoformat("2026-08-22T10:00:00+08:00")
        )
    )


def test_task_mutation_is_projected_as_its_own_semantic_item() -> None:
    command = Command(
        type=CommandType.MODIFY,
        semantic_operation="PUBLISH_NOW_AND_CREATE_DRAFT",
        constraints={"publication_intent": "DRAFT_ONLY"},
        items=[
            CommandItem(
                topic="Redis",
                requirements=["写一篇 Redis 草稿"],
                capabilities=["GENERATE_CONTENT"],
                constraints={"publication_intent": "DRAFT_ONLY"},
            )
        ],
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "SCHEDULE",
                "resource_id": "schedule-java",
                "label": "Java 学习",
            },
            desired_changes={"semantic_action": "PUBLISH_NOW"},
        )],
    )

    state = _coordinator()._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert [(item.topic, item.publication_intent) for item in state.items] == [
        ("Redis", "DRAFT_ONLY"),
        ("Java 学习", "IMMEDIATE_PUBLISH"),
    ]
    assert state.temporal_kind == "NOW"
    assert state.publication_intent == "MIXED"
    assert state.items[1].target_reference["resource_id"] == "schedule-java"


def test_schedule_dependency_keeps_future_time_owned_by_mutation_item() -> None:
    command = Command(
        type=CommandType.CREATE,
        constraints={"publication_intent": "DRAFT_ONLY"},
        items=[CommandItem(
            topic="RAG",
            requirements=["创建一篇 RAG 草稿"],
            constraints={"publication_intent": "DRAFT_ONLY"},
        )],
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "SCHEDULE", "label": "Redis 面试"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "下周一上午",
            },
        )],
    )

    state = _coordinator()._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert len(state.items) == 2
    assert state.items[1].temporal_kind == "UNRESOLVED"
    assert state.items[1].target_reference["label"] == "Redis 面试"
    assert state.publication_intent == "MIXED"
    assert state.clarification_required is True


def test_title_only_mutation_does_not_inherit_schedule_hint() -> None:
    command = Command(
        type=CommandType.MODIFY,
        semantic_operation="UPDATE_DRAFT",
        constraints={"publication_intent": "SCHEDULED_PUBLISH"},
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "DRAFT", "label": "Java 学习"},
            desired_changes={
                "semantic_action": "UPDATE_DRAFT",
                "title": "并发基础",
            },
        )],
    )

    state = _coordinator()._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert len(state.items) == 1
    assert state.items[0].publication_intent == ""
    assert state.temporal_kind == "NONE"
    assert state.publication_intent == ""


def test_identifier_mutation_is_not_projected_twice_when_action_is_unique() -> None:
    command = Command(
        type=CommandType.MODIFY,
        items=[CommandItem(
            item_key="delete_post",
            operation="DELETE",
            capabilities=["DELETE_POST"],
            requirements=["删除帖子 123"],
        )],
        task_changes=[TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            change_id="delete_post_123",
            target_reference={"kind": "POST", "id": "123"},
            desired_changes={"semantic_action": "DELETE_POST"},
        )],
    )

    state = _coordinator()._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert len(state.items) == 1
    assert state.items[0].capabilities == ["DELETE_POST"]


def test_same_action_siblings_are_not_collapsed_without_structured_pairing() -> None:
    command = Command(
        type=CommandType.MODIFY,
        items=[
            CommandItem(item_key="delete_a", operation="DELETE", capabilities=["DELETE_POST"]),
            CommandItem(item_key="delete_b", operation="DELETE", capabilities=["DELETE_POST"]),
        ],
        task_changes=[
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                change_id="mutation_a",
                target_reference={"kind": "POST", "id": "a"},
                desired_changes={"semantic_action": "DELETE_POST"},
            ),
            TaskDelta(
                operation=TaskDeltaOperation.UPDATE_GOAL,
                change_id="mutation_b",
                target_reference={"kind": "POST", "id": "b"},
                desired_changes={"semantic_action": "DELETE_POST"},
            ),
        ],
    )

    state = _coordinator()._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert len(state.items) == 4
