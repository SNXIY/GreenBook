"""Phase 5.5 behavioral evaluation runner tests."""

import pytest
from greenbook_evaluation.dataset import GOLDEN_CASES
from greenbook_evaluation.models import EvalCase
from greenbook_evaluation.runner import EvaluationRunner


@pytest.mark.asyncio
async def test_evaluation_runner_checks_behavior_and_trace() -> None:
    case = EvalCase(
        case_id="eval-create",
        category="COMMAND_GOAL",
        user_message="写一篇Java文章",
        expected_command="CREATE",
        expected_goals=["create_article"],
        expected_tools=["content.create_draft"],
        expected_task_state="COMPLETED",
    )
    result = await EvaluationRunner().run_case(
        case,
        actual={
            "command": "CREATE",
            "goals": ["create_article"],
            "tools": ["content.create_draft"],
            "task_state": "COMPLETED",
            "trace": {
                "conversation_id": "c-1",
                "task_id": "t-1",
                "plan_version": 1,
            },
        },
    )
    assert result.passed is True
    assert result.trace["task_id"] == "t-1"
    assert result.failure_categories == []


@pytest.mark.asyncio
async def test_evaluation_runner_classifies_bad_behavior() -> None:
    case = EvalCase(
        case_id="eval-policy",
        category="SAFETY",
        user_message="发布文章",
        expected_command="CREATE",
        expected_side_effects=["NO_DUPLICATE_PUBLICATION"],
    )
    result = await EvaluationRunner().run_case(
        case,
        actual={"command": "QUERY", "duplicate_side_effect": True},
    )
    assert result.passed is False
    assert "COMMAND_ERROR" in result.failure_categories
    assert "RECOVERY_ERROR" in result.failure_categories


def test_golden_cases_cover_the_required_community_scenarios() -> None:
    assert len(GOLDEN_CASES) >= 12
    assert {case.case_id for case in GOLDEN_CASES} >= {
        "community-create-java",
        "community-research-create-schedule",
        "community-target-ambiguous",
        "community-idempotent-publish",
    }
