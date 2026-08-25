"""Contract tests for the independent Business Acceptance Set."""

from __future__ import annotations

import asyncio

from greenbook_evaluation.business import BusinessAcceptanceEvaluator
from greenbook_evaluation.cases.business_acceptance import BUSINESS_ACCEPTANCE_CASES
from greenbook_evaluation.cases.semantic_baseline import SEMANTIC_BASELINE_CASES


def test_business_acceptance_set_is_independent_and_complete() -> None:
    ids = [case.case_id for case in BUSINESS_ACCEPTANCE_CASES]
    stress_ids = {case.case_id for case in SEMANTIC_BASELINE_CASES}

    assert len(ids) == 50
    assert len(ids) == len(set(ids))
    assert not stress_ids.intersection(ids)
    assert all(case.expected_semantic_state for case in BUSINESS_ACCEPTANCE_CASES)
    assert {case.category for case in BUSINESS_ACCEPTANCE_CASES} == {
        "CREATE_DRAFT", "PUBLISH_NOW", "SCHEDULE", "MULTI_OBJECTIVE", "SEARCH",
        "SEARCH_CREATE", "REVISE", "CROSS_TURN", "CANCEL", "DELETE_HITL",
        "CLARIFICATION", "DURABLE", "OWNERSHIP",
    }


def test_business_acceptance_metric_aggregation_excludes_infra_errors() -> None:
    class Adapter:
        async def run_case(self, case):
            if case.case_id.endswith("-2"):
                raise TimeoutError("provider timeout")
            return {"semantic_state": case.expected_semantic_state}

    report = asyncio.run(BusinessAcceptanceEvaluator(Adapter()).evaluate(BUSINESS_ACCEPTANCE_CASES[:3]))

    assert report.case_count == 3
    assert report.infra_error_count == 1
    assert report.valid_case_count == 2
    assert report.pass_count == 2
    assert report.fail_count == 0
    assert report.semantic_business_correctness == 1.0
    assert report.metrics["semantic_business_correctness"] == {"correct": 2, "total": 2}
