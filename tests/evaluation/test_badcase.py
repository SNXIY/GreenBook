"""Phase 6.3 Stage 3 tests for BadCase analysis."""

from __future__ import annotations

from greenbook_evaluation.analyzer import FailureAnalyzer
from greenbook_evaluation.badcase import (
    BadCase,
    BadCaseStatus,
    BadCaseStore,
    CaseLevelStatus,
    FailureType,
)
from greenbook_evaluation.metrics import MetricsCalculator
from greenbook_evaluation.models import EvalCheck, EvalResult

# ── BadCase model ─────────────────────────────────────────────────

def test_badcase_model() -> None:
    bc = BadCase(
         case_id="test-1", category="COMMAND",
        failure_type=FailureType.WRONG_CATEGORY,
        failure_reason="expected CREATE_CONTENT, got QUERY_INFO",
    )
    assert bc.failure_type == FailureType.WRONG_CATEGORY
    assert bc.case_id == "test-1"


def test_badcase_status_update_is_small_and_explicit() -> None:
    store = BadCaseStore()
    store.save(BadCase(case_id="rc-02-case-5", root_cause_id="RC-02"))

    updated = store.update_status(
        "rc-02-case-5", BadCaseStatus.FIXED, root_cause_id="RC-02"
    )

    assert updated is not None
    assert updated.status == BadCaseStatus.FIXED
    assert updated.root_cause_id == "RC-02"
    assert store.list_cases()[0].status == BadCaseStatus.FIXED


def test_case_level_ledger_deduplicates_assertions_and_excludes_invalid_eval() -> None:
    store = BadCaseStore()
    store.save(
        BadCase(
            case_id="case-1",
            assertion_id="case-1:semantic_state",
            status=BadCaseStatus.FIXED,
            root_cause_id="RC-01",
        )
    )
    store.save(
        BadCase(
            case_id="case-1",
            assertion_id="case-1:temporal_resolution",
            status=BadCaseStatus.OPEN,
            root_cause_id="RC-04",
        )
    )
    store.register_case("case-1")
    store.set_case_status("case-1", CaseLevelStatus.OPEN_AGENT, historical_root_causes=["RC-01", "RC-04"])
    store.set_case_status("case-2", CaseLevelStatus.INVALID_EVAL, historical_root_causes=["RC-06"])
    store.set_case_status("case-3", CaseLevelStatus.PASS)

    assert store.case_level_counts() == {
        "PASS": 1,
        "OPEN_AGENT": 1,
        "FIXED": 0,
        "INVALID_EVAL": 1,
        "UNCERTAIN": 0,
    }
    assert [item.assertion_id for item in store.open_agent_assertions()] == [
        "case-1:temporal_resolution"
    ]
    # Two assertions are retained in historical storage, but only one case
    # contributes to the case-level OPEN_AGENT count.
    assert len(store.list_cases()) == 2


def test_multi_assertion_status_update_requires_assertion_identity() -> None:
    store = BadCaseStore()
    store.save(BadCase(case_id="case-1", assertion_id="case-1:a"))
    store.save(BadCase(case_id="case-1", assertion_id="case-1:b"))

    assert store.update_status("case-1", BadCaseStatus.FIXED) is None
    updated = store.update_status(
        "case-1",
        BadCaseStatus.FIXED,
        assertion_id="case-1:a",
        root_cause_id="RC-02",
    )
    assert updated is not None
    assert updated.assertion_id == "case-1:a"
    assert updated.status == BadCaseStatus.FIXED


def test_reconcile_cases_closes_an_explicit_eval_universe() -> None:
    store = BadCaseStore()
    case_ids = [f"golden-{index}" for index in range(80)]
    statuses = {case_id: CaseLevelStatus.PASS for case_id in case_ids}
    statuses.update({
        case_ids[0]: CaseLevelStatus.OPEN_AGENT,
        case_ids[1]: CaseLevelStatus.FIXED,
        case_ids[2]: CaseLevelStatus.INVALID_EVAL,
        case_ids[3]: CaseLevelStatus.UNCERTAIN,
    })

    entries = store.reconcile_cases(case_ids, statuses)

    assert len(entries) == 80
    assert sum(store.case_level_counts().values()) == 80
    assert store.case_level_counts()[CaseLevelStatus.PASS.value] == 76


def test_failure_type_enum_values() -> None:
    assert FailureType.WRONG_CATEGORY.value == "WRONG_CATEGORY"
    assert FailureType.OVER_SPLIT.value == "OVER_SPLIT"
    assert FailureType.UNDER_SPLIT.value == "UNDER_SPLIT"
    assert FailureType.WRONG_TOOL.value == "WRONG_TOOL"


def test_failure_analyzer_emits_stable_assertion_ids_per_case() -> None:
    result = EvalResult(
        case_id="case-assertions",
        category="SEMANTIC",
        user_message="input",
        passed=False,
        checks=[
            EvalCheck(check="semantic_state", expected="A", actual="B", ok=False),
            EvalCheck(check="temporal_resolution", expected=True, actual=False, ok=False),
        ],
    )

    assertions = FailureAnalyzer.analyze(result)

    assert [item.assertion_id for item in assertions] == [
        "case-assertions:semantic_state",
        "case-assertions:temporal_resolution",
    ]


# ── FailureAnalyzer classification ────────────────────────────────

def test_command_wrong_type() -> None:
    """Goal category mismatch → WRONG_CATEGORY."""
    result = EvalResult(
        case_id="i1", category="COMMAND", passed=False,
        checks=[
            EvalCheck(check="command.type",
                      expected="CREATE", actual="QUERY", ok=False),
            EvalCheck(check="command.action",
                      expected="NEW", actual="NEW", ok=True),
        ],
    )
    bad_cases = FailureAnalyzer.analyze(result)
    assert len(bad_cases) >= 1
    assert any(bc.failure_type == FailureType.WRONG_CATEGORY for bc in bad_cases)


def test_command_wrong_action() -> None:
    """Relation mismatch → WRONG_RELATION."""
    result = EvalResult(
        case_id="i2", category="COMMAND", passed=False,
        checks=[
            EvalCheck(check="command.action",
                      expected="NEW", actual="MODIFY", ok=False),
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
    result = EvalResult(case_id="p1", category="COMMAND", passed=True)
    bad_cases = FailureAnalyzer.analyze(result)
    assert len(bad_cases) == 0


# ── FailureAnalyzer summary ───────────────────────────────────────

def test_failure_summary() -> None:
    bad_cases = [
        BadCase(case_id="a", category="COMMAND",
                failure_type=FailureType.WRONG_CATEGORY,
                failure_reason="x"),
        BadCase(case_id="b", category="COMMAND",
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
        EvalResult(case_id="ok", category="COMMAND", passed=True),
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
        EvalResult(case_id="a", category="COMMAND", passed=True),
        EvalResult(case_id="b", category="COMMAND", passed=True),
    ]
    report = MetricsCalculator.compute(results, "test")
    assert len(report.bad_cases) == 0
    assert report.failure_summary == {}
