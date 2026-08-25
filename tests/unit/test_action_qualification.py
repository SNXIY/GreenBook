from types import SimpleNamespace

from greenbook_agent_core.actionloop.qualification import (
    guard_action,
)


def test_resumed_objective_uses_persisted_temporal_resolution() -> None:
    objective = SimpleNamespace(
        objective_id="objective-1",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        related_resource_ids=["draft-1"],
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "run_at": "2026-08-21T03:50:00Z",
        },
        status="WAITING",
    )
    task = SimpleNamespace(
        resource_index=[
            {
                "resource_id": "draft-1",
                "resource_kind": "DRAFT",
                "objective_id": "objective-1",
            },
        ],
        execution_refs=[],
    )

    result = guard_action(
        "CREATE_SCHEDULE",
        objective,
        task,
        arguments={"run_at": "2026-08-21T03:50:00Z"},
    )

    assert result.allowed is True


def test_explicit_cross_turn_schedule_target_overrides_stale_draft_state() -> None:
    objective = SimpleNamespace(
        objective_id="objective-1",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["draft-1", "schedule-1"],
        related_operations=[],
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "run_at": "2026-08-22T08:00:00Z",
        },
        status="COMPLETED",
    )
    task = SimpleNamespace(
        resource_index=[
            {"resource_id": "draft-1", "resource_kind": "DRAFT", "objective_id": "objective-1"},
            {"resource_id": "schedule-1", "resource_kind": "SCHEDULE", "objective_id": "objective-1"},
        ],
        execution_refs=[],
    )
    command = SimpleNamespace(
        target_resolution="RESOLVED",
        # An aggregate resolver projection can still point at the Task.  The
        # explicit mutation refs below are the authoritative write targets.
        resolved_target={"kind": "TASK", "id": "task-1"},
        target=None,
        task_changes=[
            SimpleNamespace(
                desired_changes={"semantic_action": "UPDATE_SCHEDULE"},
                target_reference={"kind": "SCHEDULE", "id": "schedule-1"},
            )
        ],
    )

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        task,
        command=command,
        arguments={
            "schedule_id": "schedule-1",
            "run_at": "2026-08-22T08:00:00Z",
        },
    )

    assert result.allowed is True


def test_durable_resource_binding_wins_over_observation_projection_for_writes() -> None:
    objective = SimpleNamespace(
        objective_id="objective-1",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["schedule-1"],
        related_operations=[],
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "run_at": "2026-08-22T07:00:00Z",
        },
        status="IN_PROGRESS",
    )
    task = SimpleNamespace(
        resource_index=[
            {"resource_id": "schedule-1", "resource_kind": "SCHEDULE", "objective_id": "objective-1"},
        ],
        execution_refs=[],
    )
    command = SimpleNamespace(
        target_resolution="RESOLVED",
        resolved_target={"kind": "SCHEDULE", "resource_id": "schedule-1"},
        target=None,
        task_changes=[],
    )
    state = {
        "resources": [
            # This is an observation projection, not the durable binding.
            {"resource_id": "schedule-1", "resource_kind": "SCHEDULE", "objective_id": "other-objective"},
        ],
    }

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        state | {"task": task},
        command=command,
        arguments={
            "schedule_id": "schedule-1",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )

    assert result.allowed is True


def test_selected_persisted_mutation_can_update_a_completed_objective() -> None:
    objective = SimpleNamespace(
        objective_id="objective-1",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["schedule-1"],
        related_operations=[],
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "run_at": "2026-08-22T07:00:00Z",
        },
        status="COMPLETED",
    )
    task = SimpleNamespace(
        resource_index=[
            {"resource_id": "schedule-1", "resource_kind": "SCHEDULE", "objective_id": "objective-1"},
        ],
        execution_refs=[],
    )
    command = SimpleNamespace(
        target_resolution="RESOLVED",
        resolved_target=None,
        target=None,
        task_changes=[
            SimpleNamespace(
                desired_changes={"semantic_action": "UPDATE_SCHEDULE"},
                target_reference={"kind": "SCHEDULE", "id": "schedule-1"},
            )
        ],
    )

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        task,
        command=command,
        arguments={
            "schedule_id": "schedule-1",
            "run_at": "2026-08-22T07:00:00Z",
        },
        mutation_plan_selected=True,
    )

    assert result.allowed is True


def test_multi_target_mutation_accepts_only_the_selected_objective_resource() -> None:
    objective = SimpleNamespace(
        objective_id="objective-1",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["schedule-1"],
        related_operations=[],
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "run_at": "2026-08-22T07:00:00Z",
        },
        status="COMPLETED",
    )
    task = SimpleNamespace(
        resource_index=[
            {"resource_id": "schedule-1", "resource_kind": "SCHEDULE", "objective_id": "objective-1"},
            {"resource_id": "schedule-2", "resource_kind": "SCHEDULE", "objective_id": "objective-2"},
        ],
        execution_refs=[],
    )
    command = SimpleNamespace(
        target_resolution="RESOLVED",
        resolved_target=None,
        target=None,
        task_changes=[
            SimpleNamespace(
                desired_changes={"semantic_action": "UPDATE_SCHEDULE"},
                target_reference={"kind": "SCHEDULE", "id": "schedule-1"},
            ),
            SimpleNamespace(
                desired_changes={"semantic_action": "UPDATE_SCHEDULE"},
                target_reference={"kind": "SCHEDULE", "id": "schedule-2"},
            ),
        ],
    )

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        task,
        command=command,
        arguments={
            "schedule_id": "schedule-1",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )

    assert result.allowed is True


def test_selected_exact_target_cannot_fallback_to_another_objective() -> None:
    objective = SimpleNamespace(
        objective_id="objective-b",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["schedule-b"],
        constraints={
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
            "run_at": "2026-08-22T07:00:00Z",
        },
    )
    task = SimpleNamespace(
        resource_index=[
            {"resource_id": "schedule-a", "resource_kind": "SCHEDULE", "objective_id": "objective-a"},
            {"resource_id": "schedule-b", "resource_kind": "SCHEDULE", "objective_id": "objective-b"},
        ],
        execution_refs=[],
    )

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        task,
        arguments={
            "schedule_id": "schedule-a",
            "run_at": "2026-08-22T07:00:00Z",
        },
    )

    assert result.allowed is False
    assert result.code == "OWNERSHIP_MISMATCH"


def test_cross_turn_mutation_can_use_predecessor_owned_resource() -> None:
    objective = SimpleNamespace(
        objective_id="mutation-1",
        required_capabilities=["MANAGE_DRAFT"],
        related_resource_ids=["draft-1"],
        constraints={"target_objective_id": "objective-a"},
    )
    task = SimpleNamespace(
        resource_index=[
            {"resource_id": "draft-1", "resource_kind": "DRAFT", "objective_id": "objective-a"},
        ],
        execution_refs=[],
    )

    result = guard_action(
        "UPDATE_DRAFT",
        objective,
        task,
        arguments={"draft_id": "draft-1", "title": "new title"},
    )

    assert result.allowed is True


def test_cross_turn_mutation_can_use_prior_mutation_owned_resource() -> None:
    objective = SimpleNamespace(
        objective_id="mutation-update",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["schedule-1"],
        constraints={
            "semantic_action": "UPDATE_SCHEDULE",
            "mutation_domain": "PUBLICATION",
            "target_objective_id": "objective-source",
            "run_at": "2026-08-22T08:00:00Z",
            "temporal_kind": "FUTURE",
        },
    )
    prior_mutation = SimpleNamespace(
        objective_id="mutation-create-schedule",
        related_resource_ids=["draft-1", "schedule-1"],
        constraints={
            "semantic_action": "CREATE_SCHEDULE",
            "mutation_domain": "PUBLICATION",
            "target_objective_id": "objective-source",
        },
    )
    task = SimpleNamespace(
        objectives=[objective, prior_mutation],
        resource_index=[
            {
                "resource_id": "schedule-1",
                "resource_kind": "SCHEDULE",
                "objective_id": "mutation-create-schedule",
            },
        ],
        execution_refs=[],
    )

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        task,
        arguments={
            "schedule_id": "schedule-1",
            "run_at": "2026-08-22T08:00:00Z",
        },
    )

    assert result.allowed is True


def test_cross_turn_mutation_cannot_borrow_prior_mutation_resource_from_other_lineage() -> None:
    objective = SimpleNamespace(
        objective_id="mutation-update",
        required_capabilities=["MANAGE_SCHEDULE"],
        related_resource_ids=["schedule-1"],
        constraints={
            "semantic_action": "UPDATE_SCHEDULE",
            "mutation_domain": "PUBLICATION",
            "target_objective_id": "objective-source-a",
            "run_at": "2026-08-22T08:00:00Z",
            "temporal_kind": "FUTURE",
        },
    )
    prior_mutation = SimpleNamespace(
        objective_id="mutation-create-schedule",
        related_resource_ids=["schedule-1"],
        constraints={
            "semantic_action": "CREATE_SCHEDULE",
            "mutation_domain": "PUBLICATION",
            "target_objective_id": "objective-source-b",
        },
    )
    task = SimpleNamespace(
        objectives=[objective, prior_mutation],
        resource_index=[
            {
                "resource_id": "schedule-1",
                "resource_kind": "SCHEDULE",
                "objective_id": "mutation-create-schedule",
            },
        ],
        execution_refs=[],
    )

    result = guard_action(
        "UPDATE_SCHEDULE",
        objective,
        task,
        arguments={
            "schedule_id": "schedule-1",
            "run_at": "2026-08-22T08:00:00Z",
        },
    )

    assert result.allowed is False
    assert result.code == "OWNERSHIP_MISMATCH"
