"""Focused tests for the migrated semantic evaluation contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from greenbook_evaluation.canonical import (
    canonical_semantic_result,
    semantic_mapping_matches,
)
from greenbook_evaluation.cases.semantic_baseline import SEMANTIC_BASELINE_CASES
from greenbook_evaluation.semantic import ProductionSemanticAdapter


def test_golden_cases_have_independent_expected_values_and_real_history() -> None:
    create = next(case for case in SEMANTIC_BASELINE_CASES if case.case_id == "semantic-create_revise-1")
    revise = next(case for case in SEMANTIC_BASELINE_CASES if case.case_id == "semantic-create_revise-5")
    multi_counts = {
        case.expected_semantic_state["objective_count"]
        for case in SEMANTIC_BASELINE_CASES
        if case.category == "MULTI_OBJECTIVE"
    }
    cross_turn = next(case for case in SEMANTIC_BASELINE_CASES if case.category == "CROSS_TURN")

    assert create.expected_semantic_state["action_family"] == "CREATE"
    assert revise.expected_semantic_state["action_family"] == "REVISE"
    assert multi_counts == {2, 3}
    assert cross_turn.conversation_turns == [
        {"role": "user", "content": "Create a short post about agents"}
    ]
    assert cross_turn.user_message not in {
        turn["content"] for turn in cross_turn.conversation_turns
    }
    assert cross_turn.setup_context["targets"]


def test_canonical_projection_uses_product_semantics_over_internal_operation() -> None:
    actual = canonical_semantic_result(
        {
            "operation": "QUERY",
            "semantic_operation": "SEARCH",
            "publication_intent": "",
            "temporal_kind": "NONE",
            "temporal_resolved": False,
            "clarification_required": False,
            "objectives": [{"operation": "CREATE"}],
        }
    )

    assert actual["action_family"] == "SEARCH"
    assert actual["objective_count"] == 1
    assert actual["publication_mode"] == "NONE"


def test_canonical_projection_counts_multi_items_and_normalizes_mixed_modes() -> None:
    actual = canonical_semantic_result({
        "operation": "CREATE",
        "semantic_operation": "CREATE",
        "publication_intent": "",
        "temporal_kind": "NONE",
        "temporal_resolved": False,
        "clarification_required": False,
        "items": [
            {"publication_intent": "", "temporal_kind": "NONE"},
            {"publication_intent": "SCHEDULED_PUBLISH", "temporal_kind": "FUTURE"},
        ],
    })

    assert actual["action_family"] == "MULTI_OBJECTIVE"
    assert actual["publication_mode"] == "MIXED"
    assert actual["temporal_kind"] == "MIXED"
    assert actual["objective_count"] == 2


def test_canonical_projection_keeps_create_outcome_separate_from_schedule_mode() -> None:
    actual = canonical_semantic_result({
        "operation": "CREATE",
        "semantic_operation": "CREATE_CONTENT_AND_SCHEDULE_PUBLISH",
        "publication_intent": "SCHEDULED_PUBLISH",
        "temporal_kind": "FUTURE",
        "temporal_resolved": True,
        "items": [{
            "topic": "Redis",
            "operation": "CREATE",
            "publication_intent": "SCHEDULED_PUBLISH",
            "temporal_kind": "FUTURE",
            "temporal_resolved": True,
        }],
    })

    assert actual["action_family"] == "CREATE"
    assert actual["publication_mode"] == "SCHEDULED"


def test_canonical_projection_keeps_all_draft_multi_objective_resolved() -> None:
    actual = canonical_semantic_result({
        "operation": "CREATE",
        "semantic_operation": "CREATE",
        "publication_intent": "DRAFT_ONLY",
        "temporal_kind": "NONE",
        "temporal_resolved": False,
        "clarification_required": False,
        "items": [
            {"publication_intent": "DRAFT_ONLY", "temporal_kind": "NONE"},
            {"publication_intent": "DRAFT_ONLY", "temporal_kind": "NONE"},
        ],
    })

    assert actual["publication_mode"] == "DRAFT_ONLY"
    assert actual["temporal_kind"] == "NONE"
    assert actual["temporal_resolved"] is False


def test_comparator_allows_only_explicit_enum_aliases() -> None:
    assert semantic_mapping_matches(
        {"publication_mode": "SCHEDULED", "temporal_kind": "FUTURE"},
        {"publication_mode": "FUTURE", "temporal_kind": "FUTURE"},
    )
    assert not semantic_mapping_matches(
        {"action_family": "CREATE"},
        {"action_family": "REVISE"},
    )


def test_canonical_target_state_preserves_not_found_distinct_from_ambiguity() -> None:
    actual = canonical_semantic_result(
        {
            "operation": "MODIFY",
            "semantic_operation": "REVISE",
            "target_state": "NOT_FOUND",
            "clarification_required": True,
            "clarification_reason": "target_unresolved",
        },
        SimpleNamespace(target_resolution="NOT_FOUND", target=None),
    )

    assert actual["target_state"] == "NOT_FOUND"
    assert actual["clarification_required"] is True


def test_canonical_target_state_keeps_ambiguous_separate() -> None:
    actual = canonical_semantic_result(
        {
            "operation": "MODIFY",
            "semantic_operation": "REVISE",
            "clarification_required": True,
            "clarification_reason": "ambiguous_target",
        },
        SimpleNamespace(target_resolution="AMBIGUOUS", target=None),
    )

    assert actual["target_state"] == "AMBIGUOUS"


@pytest.mark.asyncio
async def test_empty_input_is_a_production_invalid_outcome() -> None:
    case = next(case for case in SEMANTIC_BASELINE_CASES if case.case_id == "semantic-invalid_input-1")
    actual = await ProductionSemanticAdapter().run_case(case)

    assert actual["semantic_state"]["action_family"] == "INVALID"
    assert actual["semantic_state"]["task_expectation"] == "CLARIFY"
    assert actual["error_code"] == "COMMAND_INPUT_EMPTY"
