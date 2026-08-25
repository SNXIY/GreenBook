"""Semantic evaluation must consume the production semantic boundary."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from greenbook_evaluation.badcase import BadCaseStore, FailureType
from greenbook_evaluation.models import EvalCase
from greenbook_evaluation.semantic import ProductionSemanticAdapter, SemanticEvaluator


class _FakeCompletions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(self.payload))
                )
            ]
        )


class _FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(payload))


def _case(**overrides) -> EvalCase:
    values = {
        "case_id": "semantic-production-query",
        "category": "QUERY",
        "user_message": "How many drafts do I have?",
        "expected_semantic_state": {
            "action_family": "QUERY",
            "publication_mode": "NONE",
            "temporal_kind": "NONE",
            "temporal_resolved": False,
            "target_state": "NONE",
            "clarification_required": False,
            "objective_count": 1,
            "task_expectation": "READY",
        },
        # The canonical semantic state is the single evaluation contract;
        # legacy scalar fields are intentionally not asserted here.
        "expected_objective_count": None,
        "expected_temporal_resolution": None,
        "expected_clarification": None,
        "expected_task_state": None,
    }
    values.update(overrides)
    return EvalCase(**values)


@pytest.mark.asyncio
async def test_semantic_adapter_calls_interpreter_and_resolved_state() -> None:
    llm = _FakeLLM(
        {
            "command": "QUERY",
            "goal": "count drafts",
            "semantic_operation": "LIST_DRAFTS",
            "required_capabilities": ["LIST_DRAFTS"],
            "constraints": {},
            "needs_clarification": False,
        }
    )
    adapter = ProductionSemanticAdapter(llm=llm, model="test-model")

    actual = await adapter.run_case(_case())

    assert actual["semantic_state"]["action_family"] == "QUERY"
    assert actual["semantic_state"]["objective_count"] == 1
    assert actual["raw_semantic_state"]["operation"] == "QUERY"
    assert actual["raw_semantic_state"]["semantic_operation"] == "LIST_DRAFTS"
    assert actual["resolved_semantics"]["operation"] == "QUERY"
    assert actual["objective_count"] == 1
    assert len(llm.chat.completions.calls) == 1

    report = await SemanticEvaluator(adapter).evaluate([_case()])
    assert report.total_cases == 1
    assert report.total_passed == 1
    assert report.overall_accuracy == 1.0


@pytest.mark.asyncio
async def test_semantic_failure_is_recorded_as_a_temporal_badcase() -> None:
    llm = _FakeLLM(
        {
            "command": "QUERY",
            "goal": "count drafts",
            "semantic_operation": "LIST_DRAFTS",
            "required_capabilities": ["LIST_DRAFTS"],
            "constraints": {},
            "needs_clarification": False,
        }
    )
    store = BadCaseStore()
    case = _case(
        case_id="semantic-production-temporal-mismatch",
        expected_temporal_resolution=True,
    )

    report = await SemanticEvaluator(
        ProductionSemanticAdapter(llm=llm, model="test-model"),
        badcase_store=store,
    ).evaluate([case])

    assert report.total_passed == 0
    assert report.bad_cases
    assert store.list_cases()
    assert store.list_cases()[0].failure_type == FailureType.TEMPORAL
    assert store.list_cases()[0].input == case.user_message
