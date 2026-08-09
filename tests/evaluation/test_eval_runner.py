"""Phase 6.3 Stage 2 tests for EvalRunner."""

from __future__ import annotations

import pytest
from greenbook_evaluation.datasets import (
    DECOMPOSITION_DATASET,
    INTENT_DATASET,
)
from greenbook_evaluation.metrics import MetricsCalculator
from greenbook_evaluation.models import EvalCase, EvalCheck, EvalResult
from greenbook_evaluation.runner import EvalRunner


# ── EvalRunner instantiation ─────────────────────────────────────

def test_eval_runner_creates() -> None:
    runner = EvalRunner()
    assert runner is not None
    assert runner._ras is not None


# ── Single case run ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_single_create_case() -> None:
    case = EvalCase(
        case_id="test-create", category="INTENT",
        user_message="帮我写一篇Java文章",
        expected_intent={"goal_category": "CREATE_CONTENT", "relation": "NEW_TASK"},
    )
    runner = EvalRunner()
    result = await runner._run_one(case)

    assert result.case_id == "test-create"
    assert result.passed is True
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_run_single_create_checks_status() -> None:
    case = EvalCase(
        case_id="test-status", category="INTENT",
        user_message="帮我写一篇Java文章",
        expected_intent={"goal_category": "CREATE_CONTENT"},
        expected_status="COMPLETED",
        should_succeed=True,
    )
    runner = EvalRunner()
    result = await runner._run_one(case)
    assert result.passed is True


@pytest.mark.asyncio
async def test_direct_query_case() -> None:
    case = EvalCase(
        case_id="test-direct", category="INTENT",
        user_message="你好",
        expected_intent={"goal_category": "QUERY_INFO"},
    )
    runner = EvalRunner()
    result = await runner._run_one(case)
    assert result.passed is True


# ── INTENT_DATASET full run ──────────────────────────────────────

@pytest.mark.asyncio
async def test_run_intent_dataset() -> None:
    runner = EvalRunner()
    report = await runner.run_dataset(INTENT_DATASET, "intent")
    assert report.total_cases == 20
    assert report.overall_accuracy >= 0.80  # At least 80% pass rate
    assert "INTENT" in report.by_category


# ── DECOMPOSITION_DATASET full run ───────────────────────────────

@pytest.mark.asyncio
async def test_run_decomposition_dataset() -> None:
    runner = EvalRunner()
    report = await runner.run_dataset(DECOMPOSITION_DATASET, "decomposition")
    assert report.total_cases == 10
    # Decomposition accuracy depends on L1 keyword matching
    assert report.overall_accuracy >= 0.50
    assert "DECOMPOSITION" in report.by_category


# ── MetricsCalculator ────────────────────────────────────────────

def test_metrics_all_passed() -> None:
    results = [
        EvalResult(case_id="c1", category="INTENT", passed=True),
        EvalResult(case_id="c2", category="INTENT", passed=True),
    ]
    report = MetricsCalculator.compute(results, "test")
    assert report.overall_accuracy == 1.0
    assert report.total_passed == 2


def test_metrics_mixed() -> None:
    results = [
        EvalResult(case_id="c1", category="INTENT", passed=True),
        EvalResult(case_id="c2", category="INTENT", passed=False),
        EvalResult(case_id="c3", category="DECOMPOSITION", passed=True),
    ]
    report = MetricsCalculator.compute(results, "test")
    assert report.overall_accuracy == 2 / 3
    assert len(report.by_category) == 2
    assert report.by_category["INTENT"].accuracy == 0.5
    assert report.by_category["DECOMPOSITION"].accuracy == 1.0


def test_metrics_empty() -> None:
    report = MetricsCalculator.compute([], "test")
    assert report.total_cases == 0
