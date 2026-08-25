"""Phase 5.5 behavioral evaluation runner tests."""

import pytest
from greenbook_evaluation.dataset import BASELINE_CASES, GOLDEN_CASES
from greenbook_evaluation.cases.semantic_baseline import SEMANTIC_BASELINE_CASES
from greenbook_evaluation.badcase import BadCaseStore, CaseLevelStatus
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


def test_baseline_dataset_covers_runtime_acceptance_categories() -> None:
    assert {case.category for case in BASELINE_CASES} == {
        "SINGLE_STEP",
        "MULTI_STEP",
        "MULTI_OBJECTIVE",
        "CROSS_TURN",
        "AMBIGUITY",
        "TEMPORAL",
        "IDEMPOTENCY",
        "RESUME",
    }


def test_semantic_baseline_has_five_structured_variants_per_category() -> None:
    from collections import Counter

    counts = Counter(case.category for case in SEMANTIC_BASELINE_CASES)

    assert len(SEMANTIC_BASELINE_CASES) == 80
    assert all(count == 5 for count in counts.values())
    assert all(case.expected_semantic_state is not None for case in SEMANTIC_BASELINE_CASES)


@pytest.mark.asyncio
async def test_failed_evaluation_is_promoted_to_badcase_and_reports_runtime_metrics() -> None:
    case = EvalCase(
        case_id="baseline-failure",
        category="IDEMPOTENCY",
        user_message="retry",
        expected_side_effects=["NO_DUPLICATE_PUBLICATION"],
    )
    report = await EvaluationRunner().run_cases(
        [case],
        handler=lambda _case: {
            "task_state": "FAILED",
            "duplicate_side_effect": True,
            "objective_success_rate": 0.0,
            "actionloop_iterations": 7,
            "replan_count": 1,
            "trace": {},
        },
    )
    assert report.total_passed == 0
    assert report.bad_cases
    assert report.metrics["duplicate_write_count"] == 1.0
    assert report.metrics["avg_actionloop_iterations"] == 7.0
    assert report.metrics["replan_count"] == 1.0


@pytest.mark.asyncio
async def test_semantic_baseline_metrics_remain_structured_and_safe() -> None:
    cases = SEMANTIC_BASELINE_CASES[:2]
    report = await EvaluationRunner().run_cases(
        cases,
        handler=lambda case: {
            "semantic_state": case.expected_semantic_state,
            "objective_count": case.expected_objective_count,
            "temporal_resolved": case.expected_temporal_resolution,
            "clarification": case.expected_clarification,
            "task_state": case.expected_task_state,
            "latency_ms": 8.0,
            "llm_calls": 1,
        },
    )

    assert report.metrics["semantic_state_accuracy"] == 1.0
    assert report.metrics["unsafe_action_rate"] == 0.0
    assert report.metrics["avg_llm_calls"] == 1.0


@pytest.mark.asyncio
async def test_runner_registers_pass_and_untriaged_failure_at_case_level() -> None:
    store = BadCaseStore()
    cases = [
        EvalCase(case_id="ledger-pass", category="SEMANTIC", expected_task_state="COMPLETED"),
        EvalCase(case_id="ledger-failure", category="SEMANTIC", expected_task_state="COMPLETED"),
    ]

    async def handler(case: EvalCase):
        return {"task_state": "COMPLETED" if case.case_id == "ledger-pass" else "FAILED"}

    report = await EvaluationRunner(badcase_store=store).run_cases(cases, handler=handler)

    statuses = {entry.case_id: entry.status for entry in store.list_case_ledger()}
    assert statuses == {
        "ledger-pass": CaseLevelStatus.PASS,
        "ledger-failure": CaseLevelStatus.UNCERTAIN,
    }
    assert report.case_level_counts[CaseLevelStatus.PASS.value] == 1
    assert report.case_level_counts[CaseLevelStatus.UNCERTAIN.value] == 1
    assert report.open_assertion_count == 0
