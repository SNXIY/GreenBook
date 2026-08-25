from __future__ import annotations

import pytest
from greenbook_agent_core.command.models import (
    Command,
    CommandType,
    ResolvedSemanticItem,
    ResolvedSemanticState,
)
from greenbook_agent_core.turn.commitment_poc import (
    CommitmentValidationError,
    DesiredOutcome,
    FrozenCommitment,
    HITLType,
    WorkItem,
    clarification_required,
    freeze,
    hitl_type,
    objective_from_work_item,
    project_command,
    render_confirmation,
    revalidate_draft,
    semantic_confirmation_required,
    supersede,
)
from pydantic import ValidationError


def _command_with_items(*items: ResolvedSemanticItem) -> Command:
    return Command(
        type=CommandType.CREATE,
        goal="create community content",
        raw_input="create community content",
        resolved_semantics=ResolvedSemanticState(items=list(items)),
    )


def test_project_command_keeps_one_work_item_per_business_outcome() -> None:
    command = _command_with_items(
        ResolvedSemanticItem(
            title="Java backend learning",
            topic="Java backend learning",
            capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
            publication_intent="IMMEDIATE",
        ),
        ResolvedSemanticItem(
            title="Agent development learning",
            topic="Agent development learning",
            capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            publication_intent="SCHEDULED",
            temporal_text="five minutes later",
            run_at="2026-08-21T09:05:00Z",
        ),
    )

    draft = project_command(command)

    assert [item.desired_outcome for item in draft.work_items] == [
        DesiredOutcome.PUBLISHED,
        DesiredOutcome.SCHEDULED,
    ]
    assert draft.work_items[1].canonical_run_at == "2026-08-21T09:05:00Z"
    assert all("GENERATE_CONTENT" not in item.execution_requirements for item in draft.work_items)


def test_confirmation_is_structured_and_deterministically_rendered() -> None:
    command = _command_with_items(
        ResolvedSemanticItem(
            title="Java backend learning",
            capabilities=["GENERATE_CONTENT", "PUBLISH_NOW"],
            publication_intent="IMMEDIATE",
        ),
        ResolvedSemanticItem(
            title="Agent development learning",
            capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            publication_intent="SCHEDULED",
            run_at="2026-08-21T09:05:00Z",
        ),
    )
    draft = project_command(command)

    assert semantic_confirmation_required(draft)
    rendered = render_confirmation(draft)
    assert rendered == render_confirmation(draft)
    assert "Java backend learning" in rendered
    assert "2026-08-21T09:05:00Z" in rendered
    assert hitl_type(draft) == HITLType.SEMANTIC_CONFIRMATION

    frozen = freeze(draft)
    assert isinstance(frozen, FrozenCommitment)
    assert frozen.status == "FROZEN"
    with pytest.raises(ValidationError):
        frozen.work_items = []


def test_unresolved_schedule_is_clarification_and_cannot_freeze_or_publish_now() -> None:
    command = _command_with_items(
        ResolvedSemanticItem(
            title="Agent development learning",
            capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
            publication_intent="SCHEDULED",
            temporal_text="sometime later",
        )
    )
    draft = project_command(command)

    assert draft.work_items[0].desired_outcome == DesiredOutcome.SCHEDULED
    assert draft.work_items[0].canonical_run_at is None
    assert clarification_required(draft)
    assert hitl_type(draft) == HITLType.CLARIFICATION
    with pytest.raises(CommitmentValidationError):
        freeze(draft)


def test_cross_turn_change_creates_new_version_instead_of_mutating_frozen_state() -> None:
    draft = project_command(
        _command_with_items(
            ResolvedSemanticItem(
                title="Java",
                capabilities=["GENERATE_CONTENT", "SCHEDULE_PUBLISH"],
                publication_intent="SCHEDULED",
                run_at="2026-08-21T09:05:00Z",
            )
        )
    )
    frozen = freeze(draft)
    replacement_item = frozen.work_items[0].model_copy(
        update={"canonical_run_at": "2026-08-22T06:00:00Z"}
    )

    revision = supersede(frozen, [replacement_item])

    assert revision.superseded.status == "SUPERSEDED"
    assert revision.superseded.commitment_version == 1
    assert revision.replacement.commitment_version == 2
    assert revision.replacement.supersedes_version == 1
    assert frozen.work_items[0].canonical_run_at == "2026-08-21T09:05:00Z"


def test_frontend_edit_is_revalidated_by_backend_callbacks() -> None:
    draft = project_command(
        _command_with_items(
            ResolvedSemanticItem(
                title="Java schedule",
                capabilities=["UPDATE_SCHEDULE"],
                operation="UPDATE_SCHEDULE",
                temporal_text="tomorrow at 14:00",
                target_reference={"reference": "Java schedule"},
            )
        )
    )
    result = revalidate_draft(
        draft,
        resolve_target=lambda reference: {"resource_id": "schedule-7", "kind": "SCHEDULE"},
        resolve_time=lambda expression: "2026-08-22T06:00:00Z",
    )

    assert result.work_items[0].resolved_target_ref["resource_id"] == "schedule-7"
    assert result.work_items[0].canonical_run_at == "2026-08-22T06:00:00Z"
    assert freeze(result).status == "FROZEN"


def test_work_item_adapts_to_existing_objective_without_new_persistence() -> None:
    work_item = WorkItem(
        work_item_id="wi-1",
        subject="Java",
        desired_outcome=DesiredOutcome.SCHEDULED,
        canonical_run_at="2026-08-22T06:00:00Z",
    )

    objective = objective_from_work_item(work_item, "task-1")

    assert objective.objective_id == "wi-1"
    assert objective.required_capabilities == ["GENERATE_CONTENT", "SCHEDULE_PUBLISH"]
    assert objective.constraints["run_at"] == "2026-08-22T06:00:00Z"


def test_work_item_does_not_accept_content_style_as_runtime_state() -> None:
    with pytest.raises((ValidationError, ValueError)):
        WorkItem(
            subject="Java",
            desired_outcome=DesiredOutcome.DRAFT,
            execution_requirements={"tone": True},
        )
