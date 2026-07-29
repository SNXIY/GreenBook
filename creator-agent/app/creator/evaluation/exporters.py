from __future__ import annotations

from typing import Any

from app.creator.evaluation.models import (
    CreatorEvaluationObservation,
    EvaluationCase,
    EvaluationCaseReport,
    EvaluationMetricStatus,
)


def to_langsmith_feedback(
    report: EvaluationCaseReport,
) -> tuple[dict[str, Any], ...]:
    """Return provider-neutral feedback dictionaries accepted by LangSmith clients."""

    return tuple(
        {
            "key": metric.metric.value,
            **(
                {"score": metric.score}
                if metric.status == EvaluationMetricStatus.SCORED
                else {"value": metric.status.value}
            ),
            "comment": metric.reason,
        }
        for metric in report.metrics
    )


def to_deepeval_test_case(
    case: EvaluationCase,
    observation: CreatorEvaluationObservation,
) -> dict[str, Any]:
    generation = observation.generation
    return {
        "input": case.goal,
        "actual_output": generation.body_markdown if generation else "",
        "expected_output": case.criteria.reference_answer,
        "retrieval_context": [item.text for item in observation.evidence],
        "tools_called": [
            {
                "name": call.name,
                "arguments_sha256": call.arguments_sha256,
                "status": call.status.value,
            }
            for call in observation.tool_calls
        ],
        "expected_tools": [
            {
                "name": tool.name,
                "arguments_sha256": tool.arguments_sha256,
            }
            for tool in case.criteria.expected_tools
        ],
    }


def to_ragas_record(
    case: EvaluationCase,
    observation: CreatorEvaluationObservation,
) -> dict[str, Any]:
    generation = observation.generation
    return {
        "user_input": case.goal,
        "response": generation.body_markdown if generation else "",
        "retrieved_contexts": [item.text for item in observation.evidence],
        "reference": case.criteria.reference_answer,
    }
