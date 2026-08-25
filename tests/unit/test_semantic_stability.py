"""Unit coverage for the lightweight repeated semantic evaluation."""

from __future__ import annotations

import pytest
from greenbook_evaluation.models import EvalCase
from greenbook_evaluation.stability import (
    ProductionSemanticStabilityEvaluator,
    SemanticStabilityRun,
    StabilityClassification,
    aggregate_case_stability,
    aggregate_stability_report,
    stable_fingerprint,
)


def _case(case_id: str = "semantic-stability-query") -> EvalCase:
    return EvalCase(
        case_id=case_id,
        category="QUERY",
        user_message="How many drafts do I have?",
        expected_semantic_state={
            "action_family": "QUERY",
            "publication_mode": "NONE",
            "temporal_kind": "NONE",
            "temporal_resolved": False,
            "target_state": "NONE",
            "clarification_required": False,
            "objective_count": 1,
            "task_expectation": "READY",
        },
    )


def _run(
    case_id: str,
    index: int,
    *,
    fingerprint: str,
    matches: bool,
    interpreter: str = "interpreter-a",
    canonical: dict | None = None,
) -> SemanticStabilityRun:
    return SemanticStabilityRun(
        case_id=case_id,
        run_index=index,
        canonical=canonical or {"action_family": "QUERY"},
        fingerprint=fingerprint,
        matches_expected=matches,
        interpreter_fingerprint=interpreter,
        target_fingerprint="target-a",
        temporal_fingerprint="temporal-a",
        state_fingerprint="state-a",
    )


def test_stability_fingerprint_is_order_independent() -> None:
    assert stable_fingerprint({"b": 2, "a": 1}) == stable_fingerprint({"a": 1, "b": 2})


def test_case_aggregation_separates_stable_wrong_and_unstable() -> None:
    case = _case()
    stable_wrong = aggregate_case_stability(
        case,
        [_run(case.case_id, index, fingerprint="wrong", matches=False) for index in range(1, 4)],
    )
    unstable = aggregate_case_stability(
        case,
        [
            _run(case.case_id, 1, fingerprint="a", matches=True),
            _run(case.case_id, 2, fingerprint="b", matches=False, interpreter="interpreter-b"),
        ],
    )

    assert stable_wrong.classification == StabilityClassification.STABLE_WRONG
    assert stable_wrong.correctness_rate == 0.0
    assert unstable.classification == StabilityClassification.UNSTABLE
    assert unstable.earliest_variation_layer == "INTERPRETER_NONDETERMINISM"


def test_invalid_eval_remains_visible_but_is_not_agent_quality() -> None:
    case = _case("semantic-stability-invalid")
    result = aggregate_case_stability(
        case,
        [_run(case.case_id, 1, fingerprint="same", matches=False)],
        historical_status="INVALID_EVAL",
    )

    report = aggregate_stability_report([result])

    assert result.classification == StabilityClassification.INVALID_EVAL
    assert report.invalid_eval_cases == [case.case_id]
    assert report.mean_correctness == 0.0
    assert report.consistency_rate == 0.0


class _FakeAdapter:
    def __init__(self, observations: list[dict]) -> None:
        self.observations = observations
        self.index = 0

    async def run_case(self, case: EvalCase) -> dict:
        observation = self.observations[self.index]
        self.index += 1
        return observation


def _actual(*, item_count: int, action_family: str = "QUERY") -> dict:
    items = [
        {
            "operation": "CREATE",
            "temporal_text": "",
            "constraints": {},
            "capabilities": [],
        }
        for _ in range(item_count)
    ]
    canonical = {
        "action_family": action_family,
        "publication_mode": "NONE",
        "temporal_kind": "NONE",
        "temporal_resolved": False,
        "target_state": "NONE",
        "clarification_required": False,
        "objective_count": 1,
        "task_expectation": "READY",
    }
    return {
        "semantic_state": canonical,
        "raw_semantic_state": {
            "semantic_operation": "LIST_DRAFTS",
            "temporal_kind": "NONE",
            "temporal_resolved": False,
            "items": [],
        },
        "command": {
            "type": "QUERY",
            "semantic_operation": "LIST_DRAFTS",
            "items": items,
        },
        "target": {},
    }


@pytest.mark.asyncio
async def test_evaluator_records_interpreter_item_count_variation() -> None:
    case = _case()
    evaluator = ProductionSemanticStabilityEvaluator(
        _FakeAdapter([
            _actual(item_count=0),
            _actual(item_count=1, action_family="MULTI_OBJECTIVE"),
        ])
    )

    result = await evaluator.evaluate_case(case, repeats=2)

    assert result.raw_item_count_distribution == {"0": 1, "1": 1}
    assert result.classification == StabilityClassification.UNSTABLE
    assert result.earliest_variation_layer == "INTERPRETER_NONDETERMINISM"
