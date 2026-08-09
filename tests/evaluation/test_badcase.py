"""Phase 6.3 Stage 3 tests for BadCase analysis."""

from __future__ import annotations

from greenbook_evaluation.analyzer import FailureAnalyzer
from greenbook_evaluation.badcase import BadCase, FailureType
from greenbook_evaluation.metrics import MetricsCalculator
from greenbook_evaluation.models import EvalCheck, EvalResult


# ── BadCase model ─────────────────────────────────────────────────

def test_badcase_model() -> None:
    bc = BadCase(
        case_id="test-1", category="INTENT",
        failure_type=FailureType.WRONG_CATEGORY,
        failure_reason="expected CREATE_CONTENT, got QUERY_INFO",
    )
    assert bc.failure_type == FailureType.WRONG_CATEGORY
    assert bc.case_id == "test-1"


def test_failure_type_enum_values() -> None:
    assert FailureType.WRONG_CATEGORY.value == "WRONG_CATEGORY"
    assert FailureType.OVER_SPLIT.value == "OVER_SPLIT"
    assert FailureType.UNDER_SPLIT.value == "UNDER_SPLIT"
    assert FailureType.WRONG_TOOL.value == "WRONG_TOOL"


# ── FailureAnalyzer classification ────────────────────────────────

def test_intent_wrong_category() -> None:
    """Goal category mismatch → WRONG_CATEGORY."""
    result = EvalResult(
        case_id="i1", category="INTENT", passed=False,
        checks=[
            EvalCheck(check="intent.goal_category",
                      expected="CREATE_CONTENT", actual="QUERY_INFO", ok=False),
            EvalCheck(check="intent.relation",
                      expected="NEW_TASK", actual="NEW_TASK", ok=True),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert len(bad_cases) >= 1
    assert any(bc.failure_type == FailureType.WRONG_CATEGORY for bc in bad_cases)


def test_intent_wrong_relation() -> None:
    """Relation mismatch → WRONG_RELATION."""
    result = EvalResult(
        case_id="i2", category="INTENT", passed=False,
        checks=[
            EvalCheck(check="intent.relation",
                      expected="NEW_TASK", actual="MODIFY_TASK", ok=False),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert any(bc.failure_type == FailureType.WRONG_RELATION for bc in bad_cases)


def test_decomposition_over_split() -> None:
    """Too many sub-tasks → OVER_SPLIT."""
    result = EvalResult(
        case_id="d1", category="DECOMPOSITION", passed=False,
        checks=[
            EvalCheck(check="sub_task_count", expected=1, actual=3, ok=False),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert any(bc.failure_type == FailureType.OVER_SPLIT for bc in bad_cases)


def test_decomposition_under_split() -> None:
    """Not enough sub-tasks → UNDER_SPLIT."""
    result = EvalResult(
        case_id="d2", category="DECOMPOSITION", passed=False,
        checks=[
            EvalCheck(check="sub_task_count", expected=3, actual=1, ok=False),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert any(bc.failure_type == FailureType.UNDER_SPLIT for bc in bad_cases)


def test_reference_wrong_task() -> None:
    """Wrong resource_id → WRONG_TASK."""
    result = EvalResult(
        case_id="r1", category="REFERENCE", passed=False,
        checks=[
            EvalCheck(check="resource_id",
                      expected="sched-a", actual="sched-b", ok=False),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert any(bc.failure_type == FailureType.WRONG_TASK for bc in bad_cases)


def test_execution_wrong_tool() -> None:
    """Wrong tool called → WRONG_TOOL."""
    result = EvalResult(
        case_id="e1", category="EXECUTION", passed=False,
        checks=[
            EvalCheck(check="tools",
                      expected=["content.create_draft"],
                      actual=["content.revise_draft"], ok=False),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert any(bc.failure_type == FailureType.WRONG_TOOL for bc in bad_cases)


def test_passed_result_no_badcases() -> None:
    result = EvalResult(case_id="p1", category="INTENT", passed=True)
    bad_cases = FailureAnalyzer.analyze(result)
    assert len(bad_cases) == 0


# ── FailureAnalyzer summary ───────────────────────────────────────

def test_failure_summary() -> None:
    bad_cases = [
        BadCase(case_id="a", category="INTENT",
                failure_type=FailureType.WRONG_CATEGORY,
                failure_reason="x"),
        BadCase(case_id="b", category="INTENT",
                failure_type=FailureType.WRONG_CATEGORY,
                failure_reason="y"),
        BadCase(case_id="c", category="DECOMPOSITION",
                failure_type=FailureType.UNDER_SPLIT,
                failure_reason="z"),
    ]
    summary = FailureAnalyzer.summary(bad_cases)
    assert summary["WRONG_CATEGORY"] == 2
    assert summary["UNDER_SPLIT"] == 1


# ── MetricsCalculator produces bad_cases ──────────────────────────

def test_report_includes_bad_cases() -> None:
    results = [
        EvalResult(case_id="ok", category="INTENT", passed=True),
        EvalResult(case_id="fail", category="DECOMPOSITION", passed=False,
                   checks=[
                       EvalCheck(check="sub_task_count",
                                 expected=2, actual=1, ok=False),
                   ]),
    ]
    report = MetricsCalculator.compute(results, "test")
    assert report.total_cases == 2
    assert report.total_passed == 1
    assert len(report.bad_cases) >= 1
    assert "UNDER_SPLIT" in report.failure_summary


def test_all_passed_report_no_badcases() -> None:
    results = [
        EvalResult(case_id="a", category="INTENT", passed=True),
        EvalResult(case_id="b", category="INTENT", passed=True),
    ]
    report = MetricsCalculator.compute(results, "test")
    assert len(report.bad_cases) == 0
    assert report.failure_summary == {}
