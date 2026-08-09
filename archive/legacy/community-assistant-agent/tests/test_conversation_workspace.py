from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.conversation_workspace import materialize_conversation_workspace
from app.domain import AdaptiveRoutingDecision, CommunityIntent
from app.llm import DeepSeekClient
from app.tools import tool_registry


NOW = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)


def run_row(
    run_id: str,
    prompt: str,
    *,
    status: str = "COMPLETED",
    minutes_ago: int = 0,
) -> dict:
    timestamp = NOW - timedelta(minutes=minutes_ago)
    return {
        "id": run_id,
        "prompt": prompt,
        "status": status,
        "intent": "content_publish",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def artifact_row(
    artifact_id: str,
    run_id: str,
    artifact_type: str,
    content: dict,
    *,
    minutes_ago: int = 0,
) -> dict:
    return {
        "id": artifact_id,
        "run_id": run_id,
        "artifact_type": artifact_type,
        "content": content,
        "created_at": NOW - timedelta(minutes=minutes_ago),
    }


def test_completed_run_with_unpublished_draft_remains_an_open_goal() -> None:
    workspace = materialize_conversation_workspace(
        conversation_id="conversation-1",
        runs=[run_row("run-create", "创作一篇 MySQL 学习帖子")],
        artifacts=[
            artifact_row(
                "artifact-draft",
                "run-create",
                "CONTENT_DRAFT",
                {
                    "draft_id": "draft-1",
                    "title": "MySQL 学习路线",
                    "content_sha256": "a" * 64,
                },
            )
        ],
        materialized_at=NOW,
    )

    assert workspace.active_goal_ref == "goal:run-create"
    assert workspace.open_loops == ["draft:draft-1"]
    assert workspace.entities[0].entity_id == "draft-1"
    assert workspace.entities[0].content_sha256 == "a" * 64


def test_publication_receipt_closes_the_matching_draft_open_loop() -> None:
    workspace = materialize_conversation_workspace(
        conversation_id="conversation-1",
        runs=[
            run_row("run-publish", "发布它", minutes_ago=0),
            run_row("run-create", "创作一篇 MySQL 学习帖子", minutes_ago=5),
        ],
        artifacts=[
            artifact_row(
                "artifact-published",
                "run-publish",
                "PUBLICATION_RECEIPT",
                {"post_id": "draft-1", "status": "PUBLISHED"},
            ),
            artifact_row(
                "artifact-draft",
                "run-create",
                "CONTENT_DRAFT",
                {"draft_id": "draft-1", "title": "MySQL 学习路线"},
                minutes_ago=5,
            ),
        ],
        materialized_at=NOW,
    )

    draft = next(item for item in workspace.entities if item.kind == "DRAFT")
    assert draft.status == "PUBLISHED"
    assert draft.actionable is False
    assert workspace.open_loops == []


def test_workspace_links_a_schedule_to_its_draft() -> None:
    workspace = materialize_conversation_workspace(
        conversation_id="conversation-1",
        runs=[run_row("run-schedule", "五分钟后发布")],
        artifacts=[
            artifact_row(
                "artifact-schedule",
                "run-schedule",
                "SCHEDULE_RECEIPT",
                {
                    "action_id": "action-1",
                    "draft_id": "draft-1",
                    "run_at": "2026-08-03T18:05:00+08:00",
                    "status": "SCHEDULED",
                },
            ),
            artifact_row(
                "artifact-draft",
                "run-schedule",
                "CONTENT_DRAFT",
                {"draft_id": "draft-1", "title": "MySQL 学习路线"},
                minutes_ago=1,
            ),
        ],
        materialized_at=NOW,
    )

    schedule = next(item for item in workspace.entities if item.kind == "SCHEDULE")
    draft = next(item for item in workspace.entities if item.kind == "DRAFT")
    assert schedule.related_refs == ["draft:draft-1"]
    assert "MySQL 学习路线" in schedule.label
    assert draft.related_refs == ["schedule:action-1"]
    assert draft.status == "SCHEDULED"


def test_latest_cancelled_schedule_no_longer_marks_draft_as_scheduled() -> None:
    workspace = materialize_conversation_workspace(
        conversation_id="conversation-1",
        runs=[run_row("run-cancel", "取消定时发布")],
        artifacts=[
            artifact_row(
                "artifact-cancelled",
                "run-cancel",
                "SCHEDULE_RECEIPT",
                {
                    "action_id": "action-1",
                    "draft_id": "draft-1",
                    "run_at": "2026-08-03T18:05:00+08:00",
                    "status": "CANCELLED",
                },
            ),
            artifact_row(
                "artifact-scheduled",
                "run-cancel",
                "SCHEDULE_RECEIPT",
                {
                    "action_id": "action-1",
                    "draft_id": "draft-1",
                    "run_at": "2026-08-03T18:05:00+08:00",
                    "status": "SCHEDULED",
                },
                minutes_ago=2,
            ),
            artifact_row(
                "artifact-draft",
                "run-cancel",
                "CONTENT_DRAFT",
                {"draft_id": "draft-1", "title": "MySQL 学习路线"},
                minutes_ago=3,
            ),
        ],
        materialized_at=NOW,
    )

    schedule = next(item for item in workspace.entities if item.kind == "SCHEDULE")
    draft = next(item for item in workspace.entities if item.kind == "DRAFT")
    assert schedule.status == "CANCELLED"
    assert schedule.actionable is False
    assert draft.status == "READY"


def test_revised_draft_supersedes_old_version_in_workspace() -> None:
    workspace = materialize_conversation_workspace(
        conversation_id="conversation-1",
        runs=[run_row("run-revise", "增加一些 MySQL 学习经验")],
        artifacts=[
            artifact_row(
                "artifact-new",
                "run-revise",
                "CONTENT_DRAFT",
                {
                    "draft_id": "draft-2",
                    "title": "MySQL 实战学习路线",
                    "supersedes_draft_id": "draft-1",
                },
            ),
            artifact_row(
                "artifact-old",
                "run-revise",
                "CONTENT_DRAFT",
                {"draft_id": "draft-1", "title": "MySQL 学习路线"},
                minutes_ago=2,
            ),
        ],
        materialized_at=NOW,
    )

    old = next(item for item in workspace.entities if item.entity_id == "draft-1")
    new = next(item for item in workspace.entities if item.entity_id == "draft-2")
    assert old.status == "SUPERSEDED"
    assert old.actionable is False
    assert new.related_refs == ["draft:draft-1"]
    assert new.actionable is True


def test_router_references_are_constrained_to_workspace_candidates() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-1",
                "kind": "DRAFT",
                "entity_id": "draft-1",
            }
        ]
    }
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="继续处理已有草稿",
        intent=CommunityIntent(
            domain="content_publish",
            goal="发布已有草稿",
            required_capabilities=["publishing"],
            confidence=0.96,
        ),
        turn_relation="CONTINUE",
        referenced_entities=["draft:draft-1", "draft:invented"],
    )

    decision = client._compile_adaptive_route(
        route,
        prompt="发布之前的 MySQL 草稿",
        conversation_workspace=workspace,
    )

    assert decision.turn_relation == "CONTINUE"
    assert decision.referenced_entities == ["draft:draft-1"]


def test_ambiguous_multiple_open_drafts_are_not_silently_selected() -> None:
    client = object.__new__(DeepSeekClient)
    workspace = {
        "active_goal_ref": "goal:run-create",
        "focus_refs": ["draft:draft-2", "draft:draft-1"],
        "entities": [
            {
                "ref": "draft:draft-2",
                "kind": "DRAFT",
                "entity_id": "draft-2",
                "source_run_id": "run-create",
                "status": "READY",
            },
            {
                "ref": "draft:draft-1",
                "kind": "DRAFT",
                "entity_id": "draft-1",
                "source_run_id": "run-create",
                "status": "READY",
            },
        ],
    }

    assert client._workspace_draft(workspace) is None


def test_ambiguous_follow_up_is_deferred_to_target_resolver() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-2",
                "kind": "DRAFT",
                "entity_id": "draft-2",
                "label": "Java 学习路线",
                "actionable": True,
            },
            {
                "ref": "draft:draft-1",
                "kind": "DRAFT",
                "entity_id": "draft-1",
                "label": "MySQL 学习路线",
                "actionable": True,
            },
        ]
    }
    route = AdaptiveRoutingDecision(
        execution_path="ORCHESTRATED",
        classification_summary="继续处理已有草稿",
        intent=CommunityIntent(
            domain="content_publish",
            goal="发布它",
            required_capabilities=["publishing"],
            confidence=0.8,
        ),
        turn_relation="CONTINUE",
        referenced_entities=[],
    )

    decision = client._compile_adaptive_route(
        route,
        prompt="发布它",
        conversation_workspace=workspace,
    )

    assert decision.execution_path == "ORCHESTRATED"
    assert decision.referenced_entities == []
    assert decision.direct_response is None


def test_cross_turn_rewrite_does_not_bypass_artifact_revalidation() -> None:
    client = object.__new__(DeepSeekClient)
    client.registry = tool_registry
    workspace = {
        "entities": [
            {
                "ref": "draft:draft-java",
                "kind": "DRAFT",
                "entity_id": "draft-java",
                "label": "Java 并发入门",
                "actionable": True,
            }
        ]
    }
    route = AdaptiveRoutingDecision(
        execution_path="CREATOR",
        classification_summary="改写已有草稿",
        intent=CommunityIntent(
            domain="content_modify",
            goal="把已有草稿改得更适合零基础读者",
            required_capabilities=["rewrite_content"],
            confidence=0.95,
        ),
        turn_relation="MODIFY",
        referenced_entities=["draft:draft-java"],
    )

    decision = client._compile_adaptive_route(
        route,
        prompt="把它改得更适合零基础读者",
        conversation_workspace=workspace,
    )

    assert decision.execution_path == "ORCHESTRATED"
    assert decision.plan is None
    assert decision.referenced_entities == ["draft:draft-java"]
