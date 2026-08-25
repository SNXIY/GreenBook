from greenbook_agent_api.api.routes import (
    _command_payload,
    _public_target_clarification_part,
)
from greenbook_agent_core.command.models import TaskDelta
from greenbook_agent_core.command.target import TargetResolver


def test_target_clarification_normalizes_runtime_candidate_fields():
    part = _public_target_clarification_part(
        {
            "reason": "ambiguous target",
            "command": {"command_id": "command-1", "target": {}},
            "candidates": [
                {
                    "id": "task-1",
                    "kind": "POST_DRAFT",
                    "label": "Java learning draft",
                    "task_id": "task-1",
                    "resource_id": "draft-1",
                    "status": "PENDING",
                    "objective_id": "objective-internal",
                }
            ],
        }
    )

    assert part["type"] == "target_clarification"
    assert part["command"] == {"command_id": "command-1", "target": {}}
    assert part["candidates"] == [
        {
            "identity": "draft-1",
            "type": "DRAFT",
            "task_id": "task-1",
            "resource_id": "draft-1",
            "label": "Java learning draft",
            "status": "PENDING",
        }
    ]


def test_target_clarification_uses_stable_fallback_identity():
    part = _public_target_clarification_part(
        {"candidates": [{"id": "schedule-1", "kind": "SCHEDULE", "title": "Tomorrow"}]}
    )

    assert part["candidates"] == [
        {
            "identity": "schedule-1",
            "type": "SCHEDULE",
            "resource_id": "schedule-1",
            "label": "Tomorrow",
        }
    ]


def test_target_clarification_projects_task_owner_to_bound_business_resource():
    part = _public_target_clarification_part(
        {
            "command": {"target": {"kind": "POST"}},
            "candidates": [
                {
                    "id": "objective-1",
                    "kind": "TASK",
                    "task_id": "task-1",
                    "label": "Java learning",
                    "metadata": {
                        "resource_refs": [
                            {
                                "kind": "POST",
                                "resource_id": "post-1",
                                "title": "Java learning post",
                                "status": "PUBLISHED",
                            }
                        ]
                    },
                }
            ],
        }
    )

    assert part["candidates"] == [
        {
            "identity": "post-1",
            "type": "POST",
            "task_id": "task-1",
            "resource_id": "post-1",
            "label": "Java learning post",
            "status": "PUBLISHED",
        }
    ]


def test_target_clarification_drops_owner_without_requested_resource_binding():
    part = _public_target_clarification_part(
        {
            "command": {"target": {"kind": "POST"}},
            "candidates": [
                {
                    "id": "objective-1",
                    "kind": "TASK",
                    "task_id": "task-1",
                    "label": "Java draft",
                    "metadata": {
                        "resource_refs": [
                            {"kind": "DRAFT", "resource_id": "draft-1"}
                        ]
                    },
                }
            ],
        }
    )

    assert part["candidates"] == []


def test_continuation_command_accepts_public_mapping():
    command = {"type": "MODIFY", "target": {"resource_id": "post-1"}}

    assert _command_payload(command) == command


def test_resource_schedule_mutation_resolves_task_from_update_goal_envelope():
    delta = TaskDelta(
        operation="UPDATE_GOAL",
        target_reference={
            "kind": "SCHEDULE",
            "label": "Java 那篇",
            "reference_type": "NONE",
        },
        desired_changes={
            "semantic_action": "UPDATE_SCHEDULE",
            "run_at": "2026-08-23T12:00:00Z",
        },
    )

    result = TargetResolver().resolve_task_delta(
        delta,
        [
            {
                "task_id": "task-java",
                "label": "写一篇 Java 学习短帖",
                "resource_index": [
                    {
                        "resource_id": "schedule-java",
                        "resource_kind": "SCHEDULE",
                        "label": "Java 学习短帖",
                    }
                ],
            }
        ],
    )

    assert result.is_resolved
    assert result.target is not None
    assert result.target.task_id == "task-java"
    assert result.target.resource_id == "schedule-java"
