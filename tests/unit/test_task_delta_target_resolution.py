"""TaskDelta grounding contracts for the canonical TargetResolver."""

from __future__ import annotations

from types import SimpleNamespace

from greenbook_agent_core.command.models import (
    Command,
    CommandContext,
    CommandTarget,
    CommandType,
    TargetKind,
    TargetReferenceType,
    TaskDelta,
    TaskDeltaOperation,
)
from greenbook_agent_core.command.target import TargetResolutionStatus, TargetResolver


def _candidates() -> list[dict[str, str]]:
    return [
        {
            "id": "task-1",
            "task_id": "task-1",
            "kind": "TASK",
            "label": "Prepare the Agent article",
        },
        {
            "id": "goal-schedule",
            "goal_id": "goal-schedule",
            "task_id": "task-1",
            "kind": "TASK",
            "label": "Schedule the article for publication",
        },
    ]


def test_update_goal_resolves_explicit_goal_and_owner_task() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"goal_id": "goal-schedule"},
            desired_changes={"run_at": "17:00"},
        ),
        _candidates(),
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.id == "goal-schedule"
    assert resolution.target.task_id == "task-1"


def test_update_goal_resolves_strong_schedule_id_to_its_owner_task() -> None:
    candidates = _candidates()
    candidates[0]["metadata"] = {
        "resource_refs": [
            {"kind": "SCHEDULE", "resource_id": "schedule-456"},
        ],
    }
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "SCHEDULE", "id": "schedule-456"},
            desired_changes={"semantic_action": "UPDATE_SCHEDULE", "run_at": "17:00"},
        ),
        candidates,
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-1"


def test_update_goal_resolves_owner_from_real_resource_index_shape() -> None:
    # The assembled context projects a Task's durable resources as
    # ``resource_index`` (resource_id/resource_kind/task_id), not
    # ``metadata.resource_refs``.  resource->owner must read that field.
    candidates = [
        {
            "id": "task-1",
            "task_id": "task-1",
            "kind": "TASK",
            "label": "Prepare the Agent article",
            "resource_index": [
                {"resource_id": "draft-123", "resource_kind": "DRAFT", "task_id": "task-1"},
                {"resource_id": "schedule-456", "resource_kind": "SCHEDULE", "task_id": "task-1"},
            ],
        },
    ]
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "SCHEDULE", "id": "schedule-456"},
            desired_changes={"semantic_action": "CANCEL_SCHEDULE"},
            needs_target_resolution=False,
        ),
        candidates,
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-1"


def test_explicit_resource_id_preserves_resource_identity_on_owner_resolution() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "DRAFT",
                "id": "draft-123",
                "reference_type": "IDENTIFIER",
            },
            desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "Updated"},
        ),
        [
            {
                "id": "task-1",
                "task_id": "task-1",
                "kind": "TASK",
                "label": "Prepare the article",
                "resource_index": [
                    {"resource_id": "draft-123", "resource_kind": "DRAFT", "task_id": "task-1"},
                ],
            },
        ],
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-1"
    assert resolution.target.resource_id == "draft-123"


def _draft_task_candidates() -> list[dict[str, object]]:
    return [
        {
            "id": "objective-java",
            "task_id": "task-java",
            "goal_id": "objective-java",
            "objective_id": "objective-java",
            "kind": "TASK",
            "label": "Java 帖子",
            "resource_index": [{"resource_id": "draft-java", "resource_kind": "DRAFT", "label": "Java 帖子"}],
        },
        {
            "id": "objective-redis",
            "task_id": "task-redis",
            "goal_id": "objective-redis",
            "objective_id": "objective-redis",
            "kind": "TASK",
            "label": "Redis 帖子",
            "resource_index": [{"resource_id": "draft-redis", "resource_kind": "DRAFT", "label": "Redis 帖子"}],
        },
    ]


def test_generic_draft_reference_is_ambiguous_when_two_drafts_exist() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_GOAL,
            target_reference={"kind": "DRAFT", "label": "那篇草稿"},
            desired_changes={"semantic_action": "DELETE_DRAFT"},
        ),
        _draft_task_candidates(),
    )

    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert {item.task_id for item in resolution.candidates} == {"task-java", "task-redis"}


def test_generic_draft_reference_resolves_one_draft_by_kind_and_label() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_GOAL,
            target_reference={"kind": "DRAFT", "label": "Java 那篇草稿"},
            desired_changes={"semantic_action": "DELETE_DRAFT"},
        ),
        _draft_task_candidates(),
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-java"


def test_generic_draft_reference_is_not_found_when_no_draft_exists() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_GOAL,
            target_reference={"kind": "DRAFT", "label": "那篇草稿"},
            desired_changes={"semantic_action": "DELETE_DRAFT"},
        ),
        [],
    )

    assert resolution.status == TargetResolutionStatus.NOT_FOUND


def test_typed_reference_deduplicates_terminal_mutation_history_for_one_resource() -> None:
    resource_index = [{
        "resource_id": "post-1",
        "resource_kind": "POST",
        "title": "Agent post",
        "objective_id": "original-objective",
    }]
    candidates = [
        {
            "id": "original-objective",
            "goal_id": "original-objective",
            "objective_id": "original-objective",
            "task_id": "task-1",
            "kind": "TASK",
            "label": "Agent",
            "resource_index": resource_index,
        },
        {
            "id": "mutation-delete",
            "goal_id": "mutation-delete",
            "objective_id": "mutation-delete",
            "task_id": "task-1",
            "kind": "TASK",
            "label": "Delete Post",
            "status": "SUPERSEDED",
            "resource_index": resource_index,
        },
        {
            "id": "mutation-publish",
            "goal_id": "mutation-publish",
            "objective_id": "mutation-publish",
            "task_id": "task-1",
            "kind": "TASK",
            "label": "Publish Now",
            "status": "COMPLETED",
            "resource_index": resource_index,
        },
    ]

    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"kind": "POST", "reference_type": "ACTIVE"},
            desired_changes={"semantic_action": "DELETE_POST"},
        ),
        candidates,
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-1"
    assert resolution.target.resource_id == "post-1"


def test_typed_resource_label_matches_owner_resource_index_title() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "DRAFT",
                "label": "Agent study: from basics to practice",
            },
            desired_changes={"semantic_action": "UPDATE_DRAFT", "title": "Agent route"},
        ),
        [
            {
                "id": "objective-agent",
                "goal_id": "objective-agent",
                "objective_id": "objective-agent",
                "task_id": "task-agent",
                "kind": "TASK",
                "label": "Agent study",
                "resource_index": [
                    {
                        "resource_id": "draft-agent",
                        "resource_kind": "DRAFT",
                        "title": "Agent study: from basics to practice",
                    }
                ],
            }
        ],
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.id == "objective-agent"
    assert resolution.target.task_id == "task-agent"


def test_update_goal_missing_reference_resolves_only_the_single_goal() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={},
            desired_changes={"run_at": "17:00"},
        ),
        _candidates(),
        active_task_id="task-1",
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.id == "goal-schedule"


def test_unqualified_goal_mutation_does_not_use_focus_to_hide_ambiguity() -> None:
    candidates = [
        {"id": "goal-a", "goal_id": "goal-a", "task_id": "task-1", "kind": "TASK", "label": "A"},
        {"id": "goal-b", "goal_id": "goal-b", "task_id": "task-1", "kind": "TASK", "label": "B"},
    ]
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={},
            desired_changes={"semantic_action": "PUBLISH_NOW"},
        ),
        candidates,
        conversation_focus_task_id="task-1",
    )

    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert {item.id for item in resolution.candidates} == {"goal-a", "goal-b"}


def test_weak_reference_does_not_use_conversation_focus_to_hide_ambiguity() -> None:
    candidates = [
        {
            "id": "task-redis",
            "task_id": "task-redis",
            "kind": "TASK",
            "label": "Redis article",
            "updated_at": "2026-08-15T20:00:00Z",
        },
        {
            "id": "task-mysql",
            "task_id": "task-mysql",
            "kind": "TASK",
            "label": "MySQL article",
            "updated_at": "2026-08-15T10:00:00Z",
        },
    ]
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_TASK,
            target_reference={"reference_type": "ACTIVE", "label": "just now"},
        ),
        candidates,
        conversation_focus_task_id="task-mysql",
    )

    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert len(resolution.candidates) == 2


def test_weak_reference_with_multiple_candidates_requires_clarification() -> None:
    candidates = [
        {"id": "task-a", "task_id": "task-a", "kind": "TASK", "label": "A"},
        {"id": "task-b", "task_id": "task-b", "kind": "TASK", "label": "B"},
    ]
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.CANCEL_TASK,
            target_reference={"reference_type": "RECENT", "label": "just now"},
        ),
        candidates,
    )

    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert len(resolution.candidates) == 2


def test_update_goal_ambiguous_label_requires_clarification() -> None:
    candidates = _candidates()
    candidates.append({
        "id": "goal-schedule-2",
        "goal_id": "goal-schedule-2",
        "task_id": "task-2",
        "kind": "TASK",
        "label": "Schedule the article for publication",
    })
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={"label": "Schedule the article for publication"},
            desired_changes={"run_at": "17:00"},
        ),
        candidates,
    )

    assert resolution.status == TargetResolutionStatus.AMBIGUOUS


def test_task_relative_cancel_can_use_active_task() -> None:
    resolution = TargetResolver().resolve_task_delta(
        SimpleNamespace(
            operation=TaskDeltaOperation.CANCEL_TASK,
            target_reference={"reference_type": "ACTIVE"},
        ),
        _candidates(),
        active_task_id="task-1",
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-1"


def test_top_level_active_target_does_not_override_multiple_candidates() -> None:
    resolution = TargetResolver().resolve(
        Command(
            type=CommandType.MODIFY,
            target=CommandTarget(
                kind=TargetKind.POST,
                reference_type=TargetReferenceType.ACTIVE,
            ),
        ),
        CommandContext(
            active_target={"kind": "POST", "id": "post-a"},
            targets=[
                {"kind": "POST", "id": "post-a"},
                {"kind": "POST", "id": "post-b"},
            ],
        ),
    )

    assert resolution.status == TargetResolutionStatus.AMBIGUOUS
    assert {item.identity for item in resolution.candidates} == {"post-a", "post-b"}


def test_task_delta_uses_current_turn_subject_when_provider_label_is_longer() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "DRAFT",
                "label": "Agent 工程实践：从设计到落地的关键要点",
                "reference_type": "NONE",
            },
            desired_changes={"semantic_action": "PUBLISH_NOW"},
        ),
        [
            {
                "id": "task-agent",
                "task_id": "task-agent",
                "kind": "TASK",
                "label": "Agent 工程实践",
                "resource_index": [
                    {"resource_id": "draft-agent", "resource_kind": "DRAFT"},
                ],
            },
            {
                "id": "task-java",
                "task_id": "task-java",
                "kind": "TASK",
                "label": "Java 后端稳定性",
                "resource_index": [
                    {"resource_id": "draft-java", "resource_kind": "DRAFT"},
                ],
            },
        ],
        user_input="把刚才取消定时的 Agent 草稿直接发布。",
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-agent"
    assert resolution.target.resource_id == "draft-agent"


def test_typed_resource_grounding_ignores_sibling_objective_projections() -> None:
    agent_draft = {
        "resource_id": "draft-agent",
        "resource_kind": "DRAFT",
        "objective_id": "objective-agent",
    }
    java_draft = {
        "resource_id": "draft-java",
        "resource_kind": "DRAFT",
        "objective_id": "objective-java",
        "title": "Java article",
    }
    candidates = [
        {
            "id": "task-agent",
            "task_id": "task-agent",
            "kind": "TASK",
            "label": "Agent article",
            "resource_index": [agent_draft],
        },
        {
            "id": "objective-agent",
            "goal_id": "objective-agent",
            "objective_id": "objective-agent",
            "task_id": "task-agent",
            "kind": "TASK",
            "label": "Agent article",
            "resource_index": [agent_draft],
        },
        {
            "id": "mutation-schedule",
            "goal_id": "mutation-schedule",
            "objective_id": "mutation-schedule",
            "task_id": "task-agent",
            "kind": "TASK",
            "label": "Update Schedule",
            "resource_index": [
                {"resource_id": "schedule-agent", "resource_kind": "SCHEDULE", "objective_id": "mutation-schedule"},
                agent_draft,
            ],
        },
        {
            "id": "task-java",
            "task_id": "task-java",
            "kind": "TASK",
            "label": "Java article",
            "resource_index": [java_draft],
        },
        {
            "id": "objective-java",
            "goal_id": "objective-java",
            "objective_id": "objective-java",
            "task_id": "task-java",
            "kind": "TASK",
            "label": "Java article",
            "resource_index": [java_draft],
        },
        {
            # A sibling Objective can retain the Agent semantic label while
            # owning only the Java draft inherited from the Task projection.
            "id": "sibling-agent",
            "goal_id": "sibling-agent",
            "objective_id": "sibling-agent",
            "task_id": "task-java",
            "kind": "TASK",
            "label": "Agent article",
            "resource_index": [java_draft],
        },
    ]
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "DRAFT",
                "label": "Agent article full title",
                "reference_type": "NONE",
            },
            desired_changes={"semantic_action": "PUBLISH_NOW"},
        ),
        candidates,
        user_input="publish Agent draft",
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.id == "objective-agent"
    assert resolution.target.resource_id == "draft-agent"


def test_typed_task_fallback_excludes_sibling_without_requested_resource_kind() -> None:
    resolution = TargetResolver().resolve_task_delta(
        TaskDelta(
            operation=TaskDeltaOperation.UPDATE_GOAL,
            target_reference={
                "kind": "SCHEDULE",
                "label": "Agent article",
                "reference_type": "PROPERTY",
            },
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-26T10:00:00+08:00",
            },
        ),
        [
            {
                "id": "task-agent",
                "task_id": "task-agent",
                "kind": "TASK",
                "label": "Agent article",
                "resource_index": [
                    {"resource_id": "schedule-agent", "resource_kind": "SCHEDULE"},
                    {"resource_id": "draft-agent", "resource_kind": "DRAFT"},
                ],
            },
            {
                # This failed multi-objective sibling repeats the topic label
                # but owns only the Java Draft, not a Schedule.
                "id": "task-failed",
                "task_id": "task-failed",
                "kind": "TASK",
                "status": "FAILED",
                "label": "Java and Agent articles",
                "resource_index": [
                    {"resource_id": "draft-java", "resource_kind": "DRAFT"},
                ],
            },
        ],
        user_input="把 Agent 文章的发布时间改到明天上午十点。",
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.task_id == "task-agent"
    assert resolution.target.resource_id == "schedule-agent"


def test_target_contract_accepts_provider_label_and_failed_reference() -> None:
    target = CommandTarget.model_validate({
        "kind": "TASK",
        "reference_type": "FAILED",
        "label": "Agent Memory",
    })

    assert target.reference_type == TargetReferenceType.FAILED
    assert target.label == "Agent Memory"


def test_top_level_failed_reference_is_status_scoped() -> None:
    resolution = TargetResolver().resolve(
        Command(
            type=CommandType.MODIFY,
            target=CommandTarget(
                kind=TargetKind.TASK,
                id="objective-failed",
                reference_type=TargetReferenceType.FAILED,
                label="Agent Memory",
            ),
        ),
        CommandContext(targets=[
            {
                "id": "objective-failed",
                "objective_id": "objective-failed",
                "task_id": "task-failed",
                "kind": "TASK",
                "label": "Agent Memory",
                "status": "FAILED",
            },
            {
                "id": "objective-done",
                "objective_id": "objective-done",
                "task_id": "task-done",
                "kind": "TASK",
                "label": "Agent Memory",
                "status": "COMPLETED",
            },
        ]),
    )

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.id == "objective-failed"


def test_create_schedule_resolves_existing_draft_target() -> None:
    command = Command(
        type=CommandType.MODIFY,
        semantic_operation="CREATE_SCHEDULE",
        target=CommandTarget(
            kind=TargetKind.TASK,
            label="Java study draft",
            property="label",
            value="Java",
            reference_type=TargetReferenceType.PROPERTY,
        ),
        task_changes=[],
    )

    resolution = TargetResolver().resolve(command, CommandContext(
        active_target={"kind": "DRAFT", "id": "draft-java", "label": "Java study draft"},
        targets=[
            {
                "kind": "TASK",
                "id": "task-java",
                "resource_index": [
                    {"resource_id": "draft-java", "resource_kind": "DRAFT", "title": "Java study draft"}
                ],
            },
            {"kind": "DRAFT", "id": "draft-java", "label": "Java study draft"},
        ],
    ))

    assert resolution.status == TargetResolutionStatus.RESOLVED
    assert resolution.target is not None
    assert resolution.target.kind == TargetKind.DRAFT
    assert resolution.target.id == "draft-java"
