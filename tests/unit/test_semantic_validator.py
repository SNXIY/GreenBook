"""Cross-field semantic validation stays separate from resolution."""

from __future__ import annotations

from types import SimpleNamespace

from greenbook_agent_core.command import (
    Command,
    CommandTarget,
    CommandType,
    TargetKind,
    TargetReferenceType,
)
from greenbook_agent_core.command.models import CommandItem
from greenbook_agent_core.command.semantic_derivation import apply_semantic_derivation
from greenbook_agent_core.command.semantic_validator import validate_semantic_candidate


def _target(kind: TargetKind = TargetKind.SCHEDULE) -> CommandTarget:
    return CommandTarget(
        kind=kind,
        id=f"{kind.value.lower()}-1",
        task_id="task-1",
        resource_id=f"{kind.value.lower()}-1",
        reference_type=TargetReferenceType.IDENTIFIER,
    )


def _command(**overrides) -> Command:
    values = {
        "type": CommandType.MODIFY,
        "semantic_operation": "UPDATE_SCHEDULE",
        "required_capabilities": ["SCHEDULE_PUBLISH"],
        "constraints": {"publication_intent": "SCHEDULED_PUBLISH"},
        "target": _target(),
        "needs_clarification": False,
    }
    values.update(overrides)
    return Command(**values)


def test_valid_search_passes() -> None:
    result = validate_semantic_candidate(Command(
        type=CommandType.QUERY,
        semantic_operation="SEARCH",
        required_capabilities=["SEARCH_COMMUNITY"],
        constraints={"recency": "recent", "temporal_kind": "NONE"},
    ))

    assert result.valid is True
    assert result.errors == []


def test_search_only_temporal_pollution_is_invalid() -> None:
    result = validate_semantic_candidate(Command(
        type=CommandType.QUERY,
        semantic_operation="SEARCH",
        required_capabilities=["SEARCH_COMMUNITY"],
        items=[CommandItem(
            operation="CREATE",
            capabilities=["SEARCH_COMMUNITY"],
            temporal_text="tomorrow",
        )],
    ))

    assert result.valid is False
    assert any(error.code == "SEMANTIC_TEMPORAL_CONFLICT" for error in result.errors)


def test_search_create_connected_workflow_is_not_search_only() -> None:
    result = validate_semantic_candidate(Command(
        type=CommandType.CREATE,
        semantic_operation="SEARCH_CREATE",
        required_capabilities=["SEARCH_COMMUNITY", "GENERATE_CONTENT"],
        items=[CommandItem(
            operation="CREATE",
            capabilities=["SEARCH_COMMUNITY", "GENERATE_CONTENT"],
        )],
    ))

    assert result.valid is True


def test_valid_publish_now_passes() -> None:
    result = validate_semantic_candidate(_command(
        semantic_operation="PUBLISH_NOW",
        required_capabilities=["PUBLISH_NOW"],
        constraints={"publication_intent": "IMMEDIATE_PUBLISH"},
        target=_target(TargetKind.DRAFT),
    ))

    assert result.valid is True


def test_valid_resolved_schedule_passes() -> None:
    result = validate_semantic_candidate(_command(
        task_changes=[{
            "operation": "UPDATE_GOAL",
            "desired_changes": {
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "2026-08-21T06:00:00Z",
            },
        }],
    ))

    assert result.valid is True


def test_valid_unresolved_schedule_with_clarification_passes() -> None:
    result = validate_semantic_candidate(_command(
        needs_clarification=True,
        task_changes=[{
            "operation": "UPDATE_GOAL",
            "desired_changes": {
                "semantic_action": "UPDATE_SCHEDULE",
                "run_at": "when convenient",
            },
        }],
    ))

    assert result.valid is True


def test_scheduled_publication_and_publish_now_are_invalid() -> None:
    result = validate_semantic_candidate(_command(
        semantic_operation="PUBLISH_NOW",
        required_capabilities=["PUBLISH_NOW"],
        needs_clarification=True,
        target=_target(TargetKind.DRAFT),
    ))

    assert result.valid is False
    assert {error.code for error in result.errors} >= {
        "SEMANTIC_PUBLICATION_CONFLICT",
        "SEMANTIC_TEMPORAL_CONFLICT",
    }


def test_unresolved_future_and_now_temporal_are_invalid() -> None:
    result = validate_semantic_candidate(_command(
        semantic_operation="PUBLISH_NOW",
        required_capabilities=["PUBLISH_NOW"],
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "NOW",
        },
        needs_clarification=True,
        target=_target(TargetKind.DRAFT),
    ))

    assert result.valid is False
    assert any(error.code == "SEMANTIC_TEMPORAL_CONFLICT" for error in result.errors)


def test_scheduled_now_temporal_is_invalid_without_clarification_flag() -> None:
    result = validate_semantic_candidate(_command(
        constraints={
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "NOW",
        },
    ))

    assert result.valid is False
    assert any(error.code == "SEMANTIC_TEMPORAL_CONFLICT" for error in result.errors)


def test_operation_capability_publication_conflict_is_invalid() -> None:
    result = validate_semantic_candidate(_command(
        semantic_operation="UPDATE_SCHEDULE",
        required_capabilities=["PUBLISH_NOW"],
        constraints={"publication_intent": "SCHEDULED_PUBLISH"},
    ))

    assert result.valid is False
    assert any(error.code == "SEMANTIC_CAPABILITY_CONFLICT" for error in result.errors)


def test_scheduled_operation_without_schedule_capability_is_invalid() -> None:
    result = validate_semantic_candidate(_command(
        required_capabilities=[],
    ))

    assert result.valid is False
    assert any(error.code == "SEMANTIC_CAPABILITY_CONFLICT" for error in result.errors)


def test_mixed_draft_and_resolved_schedule_items_pass() -> None:
    result = validate_semantic_candidate(Command(
        type=CommandType.CREATE,
        semantic_operation="CREATE",
        required_capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
        items=[
            CommandItem(
                operation="CREATE",
                capabilities=["GENERATE_CONTENT"],
                constraints={"publication_intent": "DRAFT_ONLY"},
            ),
            CommandItem(
                operation="CREATE",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                temporal_text="tomorrow",
                constraints={
                    "publication_intent": "SCHEDULED_PUBLISH",
                    "run_at": "2026-08-21T06:00:00Z",
                },
            ),
        ],
    ))

    assert result.valid is True


def test_mixed_immediate_and_scheduled_items_keep_publication_ownership() -> None:
    result = validate_semantic_candidate(Command(
        type=CommandType.CREATE,
        semantic_operation="CREATE_AND_PUBLISH",
        required_capabilities=[
            "GENERATE_CONTENT",
            "PUBLISH_NOW",
            "SCHEDULE_PUBLISH",
        ],
        items=[
            CommandItem(
                operation="CREATE",
                capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
                constraints={"publication_intent": "IMMEDIATE_PUBLISH"},
            ),
            CommandItem(
                operation="CREATE",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                temporal_text="five minutes from now",
                constraints={
                    "publication_intent": "SCHEDULED_PUBLISH",
                    "run_at": "2026-08-21T06:00:00Z",
                },
            ),
        ],
    ))

    assert result.valid is True


def test_validator_does_not_mutate_or_resolve_target() -> None:
    command = _command()
    before = command.model_dump(mode="json")

    result = validate_semantic_candidate(command)

    assert result.valid is True
    assert command.model_dump(mode="json") == before
    assert command.target_resolution is None


class _FakeCompletions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        import json

        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(self.payload))
                )
            ]
        )


class _FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(payload))


def test_redundant_immediate_fields_remain_visible_to_validator() -> None:
    candidate = Command(
        type=CommandType.MODIFY,
        semantic_operation="PUBLISH_NOW",
        required_capabilities=["PUBLISH_NOW"],
        constraints={"publication_intent": "SCHEDULED_PUBLISH"},
        needs_clarification=True,
        target=_target(TargetKind.DRAFT),
    )

    derived = apply_semantic_derivation(candidate)

    assert derived.semantic_operation == "PUBLISH_NOW"
    assert derived.required_capabilities == ["PUBLISH_NOW"]
    assert validate_semantic_candidate(derived).valid is False
