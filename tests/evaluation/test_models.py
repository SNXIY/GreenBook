"""Phase 6.3 tests for evaluation models and datasets."""

from __future__ import annotations

from greenbook_evaluation.datasets import (
    ALL_DATASETS,
    DECOMPOSITION_DATASET,
    INTENT_DATASET,
)
from greenbook_evaluation.models import (
    CategoryMetrics,
    EvalCase,
    EvalCheck,
    EvalResult,
    EvaluationReport,
)


# ── Model tests ───────────────────────────────────────────────────

def test_eval_case_defaults() -> None:
    case = EvalCase(case_id="test-1", category="INTENT")
    assert case.case_id == "test-1"
    assert case.should_succeed is True
    assert case.expected_status == "COMPLETED"
    assert case.expected_clarification is False


def test_eval_case_with_intent() -> None:
    case = EvalCase(
        case_id="test-2", category="INTENT",
        user_message="写一篇Java文章",
        expected_intent={"goal_category": "CREATE_CONTENT"},
    )
    assert case.expected_intent is not None
    assert case.expected_intent["goal_category"] == "CREATE_CONTENT"


def test_eval_result_basic() -> None:
    result = EvalResult(case_id="test-1", category="INTENT", passed=True)
    assert result.passed is True
    assert len(result.checks) == 0


def test_eval_check_passed() -> None:
    check = EvalCheck(check="intent.goal_category",
                      expected="CREATE_CONTENT", actual="CREATE_CONTENT", ok=True)
    assert check.ok is True


def test_eval_check_failed() -> None:
    check = EvalCheck(check="intent.goal_category",
                      expected="CREATE_CONTENT", actual="IMPROVE_CONTENT", ok=False)
    assert check.ok is False


def test_evaluation_report_aggregation() -> None:
    report = EvaluationReport(
        run_id="run-1", total_cases=2, total_passed=1, overall_accuracy=0.5,
    )
    assert report.overall_accuracy == 0.5
    assert report.total_cases == 2


# ── Dataset tests ──────────────────────────────────────────────────

def test_intent_dataset_count() -> None:
    assert len(INTENT_DATASET) == 20


def test_decomposition_dataset_count() -> None:
    assert len(DECOMPOSITION_DATASET) == 10


def test_intent_dataset_has_all_categories() -> None:
    categories = {c.case_id.split("-")[1] for c in INTENT_DATASET}
    expected = {"create", "improve", "search", "publish", "cancel", "ambiguous", "direct"}
    assert categories == expected


def test_decomposition_split_cases() -> None:
    splits = [c for c in DECOMPOSITION_DATASET if c.case_id.startswith("decomp-split")]
    assert len(splits) == 5
    for c in splits:
        assert c.expected_sub_task_count is not None
        assert c.expected_sub_task_count >= 2


def test_decomposition_merge_cases() -> None:
    merges = [c for c in DECOMPOSITION_DATASET if c.case_id.startswith("decomp-merge")]
    assert len(merges) == 5
    for c in merges:
        assert c.expected_sub_task_count == 1


def test_all_datasets_catalog() -> None:
    assert "intent" in ALL_DATASETS
    assert "decomposition" in ALL_DATASETS
    assert len(ALL_DATASETS["intent"]) == 20
    assert len(ALL_DATASETS["decomposition"]) == 10


def test_case_ids_unique() -> None:
    all_cases = INTENT_DATASET + DECOMPOSITION_DATASET
    ids = [c.case_id for c in all_cases]
    assert len(ids) == len(set(ids)), f"Duplicate case_ids: {ids}"


def test_intent_create_cases_have_correct_category() -> None:
    creates = [c for c in INTENT_DATASET if c.case_id.startswith("intent-create")]
    assert len(creates) == 4
    for c in creates:
        assert c.expected_intent["goal_category"] == "CREATE_CONTENT"


def test_intent_improve_cases_have_correct_category() -> None:
    improves = [c for c in INTENT_DATASET if c.case_id.startswith("intent-improve")]
    assert len(improves) == 6
    for c in improves:
        assert c.expected_intent["goal_category"] == "IMPROVE_CONTENT"
