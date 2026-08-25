"""Compatibility projection must not become a second Interpreter."""

from __future__ import annotations

from greenbook_agent_api.services.turn_coordinator import TurnCoordinator
from greenbook_agent_core.command.models import Command, CommandItem, CommandType, TaskDelta
from greenbook_agent_core.command.semantic_derivation import apply_semantic_derivation
from greenbook_agent_core.command.semantic_validator import validate_semantic_candidate


def test_projection_preserves_explicit_candidate_fields() -> None:
    command = apply_semantic_derivation(Command(
        type=CommandType.QUERY,
        semantic_operation="list-drafts",
        required_capabilities=["LIST-DRAFTS"],
        first_action="list drafts",
    ))

    assert command.semantic_operation == "LIST_DRAFTS"
    assert command.required_capabilities == ["LIST_DRAFTS"]
    assert command.first_action == "LIST_DRAFTS"
    assert command.needs_clarification is False


def test_projection_does_not_infer_operation_or_capability_from_publication() -> None:
    command = apply_semantic_derivation(Command(
        type=CommandType.CREATE,
        constraints={"publication_intent": "SCHEDULED_PUBLISH"},
        items=[CommandItem(topic="Java")],
    ))

    assert command.semantic_operation == ""
    assert command.required_capabilities == []
    assert command.needs_clarification is False
    assert command.constraints["publication_intent"] == "SCHEDULED_PUBLISH"


def test_projection_preserves_conflict_for_validator() -> None:
    command = apply_semantic_derivation(Command(
        type=CommandType.MODIFY,
        semantic_operation="PUBLISH_NOW",
        required_capabilities=["PUBLISH_NOW"],
        constraints={"publication_intent": "SCHEDULED_PUBLISH"},
        needs_clarification=True,
    ))

    assert command.semantic_operation == "PUBLISH_NOW"
    assert command.required_capabilities == ["PUBLISH_NOW"]
    assert validate_semantic_candidate(command).valid is False


def test_search_pollution_is_not_silently_rewritten() -> None:
    command = apply_semantic_derivation(Command(
        type=CommandType.QUERY,
        semantic_operation="SEARCH_POSTS",
        required_capabilities=["SEARCH_COMMUNITY", "SCHEDULE_PUBLISH"],
        items=[CommandItem(
            operation="SEARCH",
            capabilities=["SEARCH_COMMUNITY", "SCHEDULE_PUBLISH"],
            temporal_text="some future expression",
        )],
    ))

    assert command.required_capabilities == ["SEARCH_COMMUNITY", "SCHEDULE_PUBLISH"]
    assert command.items[0].capabilities == ["SEARCH_COMMUNITY", "SCHEDULE_PUBLISH"]
    assert command.items[0].temporal_text == "some future expression"
    assert validate_semantic_candidate(command).valid is False


def test_mixed_item_publication_evidence_remains_item_scoped() -> None:
    command = apply_semantic_derivation(Command(
        type=CommandType.CREATE,
        items=[
            CommandItem(
                topic="draft",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                constraints={"publication_intent": "DRAFT_ONLY"},
            ),
            CommandItem(
                topic="scheduled",
                capabilities=["GENERATE_CONTENT"],
                constraints={"publication_intent": "SCHEDULED_PUBLISH"},
            ),
        ],
    ))

    assert command.constraints.get("publication_intent") is None
    assert "SCHEDULE_PUBLISH" in command.items[0].capabilities
    assert command.items[0].constraints["publication_intent"] == "DRAFT_ONLY"
    assert command.items[1].constraints["publication_intent"] == "SCHEDULED_PUBLISH"


def test_task_delta_is_not_promoted_to_top_level_semantics() -> None:
    command = apply_semantic_derivation(Command(
        type=CommandType.MODIFY,
        task_changes=[TaskDelta(
            operation="UPDATE_GOAL",
            target_reference={"reference_type": "ACTIVE"},
            desired_changes={
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "when convenient",
            },
        )],
    ))

    assert command.semantic_operation == ""
    assert command.required_capabilities == []


def test_coordinator_does_not_rederive_semantics_from_capabilities() -> None:
    command = Command(
        type=CommandType.CREATE,
        required_capabilities=["PUBLISH_NOW"],
        items=[CommandItem(topic="Java", capabilities=["PUBLISH_NOW"])],
    )

    state = TurnCoordinator()._resolve_semantic_state(  # noqa: SLF001
        command,
        target_resolution=None,
        timezone="Asia/Shanghai",
    )

    assert state.semantic_operation == ""
    assert state.publication_intent == ""
    assert state.capabilities == ["PUBLISH_NOW"]
