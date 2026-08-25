from __future__ import annotations

from evaluation.semantic_longtail.run_benchmark import _core_action_matches


def test_compound_revise_accepts_existing_update_delta() -> None:
    state = {
        "items": [{
            "operation": "MODIFY",
            "publication_intent": "IMMEDIATE_PUBLISH",
        }],
    }
    command = {
        "task_changes": [{
            "operation": "UPDATE_GOAL",
            "desired_changes": {"semantic_action": "PUBLISH_NOW"},
        }],
    }

    assert _core_action_matches("REVISE", "REVISE", state, command)


def test_compound_revise_rejects_create_task_for_existing_retry() -> None:
    state = {
        "items": [{
            "operation": "CREATE",
            "publication_intent": "IMMEDIATE_PUBLISH",
        }],
    }
    command = {
        "task_changes": [{
            "operation": "CREATE_TASK",
            "desired_changes": {"semantic_action": "PUBLISH_NOW"},
        }],
    }

    assert not _core_action_matches("REVISE", "REVISE", state, command)


def test_schedule_item_ownership_survives_revise_aggregate_alias() -> None:
    state = {
        "items": [{
            "operation": "UPDATE_SCHEDULE",
            "publication_intent": "SCHEDULED_PUBLISH",
        }],
    }
    command = {
        "task_changes": [{
            "operation": "UPDATE_GOAL",
            "desired_changes": {"semantic_action": "UPDATE_SCHEDULE"},
        }],
    }

    assert _core_action_matches("UPDATE_SCHEDULE", "REVISE", state, command)
