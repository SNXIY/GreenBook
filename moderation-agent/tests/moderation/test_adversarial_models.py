import pytest
from pydantic import ValidationError

from agents.moderation.adversarial import detect_agent_conflict
from moderation.schemas import (
    AdversarialAgentMetrics,
    JudgeAgentResult,
    ModerationAction,
    RiskAgentResult,
    RiskType,
    SafeAgentResult,
)


def risk_result(position: str = "LIKELY_VIOLATION") -> RiskAgentResult:
    return RiskAgentResult.model_validate(
        {
            "position": position,
            "risk_type": "ABUSE",
            "risk_score": 0.65,
            "content_evidence": ["You are stupid"],
            "arguments": ["The wording targets another person."],
            "uncertainties": ["The surrounding conversation is unavailable."],
            "suggested_action": "HUMAN_REVIEW",
        }
    )


def safe_result(position: str = "LIKELY_SAFE") -> SafeAgentResult:
    return SafeAgentResult.model_validate(
        {
            "position": position,
            "false_positive_risk": 0.7,
            "alternative_interpretations": ["The phrase may quote another speaker."],
            "missing_evidence": ["No conversation context is available."],
            "suggested_action": "HUMAN_REVIEW",
        }
    )


def test_agent_result_models_validate_bounded_scores() -> None:
    assert risk_result().risk_type == RiskType.ABUSE
    assert safe_result().suggested_action == ModerationAction.HUMAN_REVIEW

    with pytest.raises(ValidationError):
        RiskAgentResult.model_validate(risk_result().model_dump() | {"risk_score": 1.1})

    with pytest.raises(ValidationError):
        JudgeAgentResult(
            action=ModerationAction.PASS,
            risk_type=RiskType.NORMAL,
            risk_score=0.1,
            confidence=-0.1,
            reason="Invalid confidence.",
        )


def test_detect_agent_conflict_requires_opposing_positions() -> None:
    assert detect_agent_conflict(risk_result(), safe_result()) is True
    assert detect_agent_conflict(risk_result(), safe_result("UNCERTAIN")) is False
    assert detect_agent_conflict(risk_result("UNCERTAIN"), safe_result()) is False


def test_agent_metrics_restrict_trace_names_and_non_negative_values() -> None:
    metrics = AdversarialAgentMetrics(
        trace_name="risk_investigator",
        model_name="fake",
        latency_ms=12.5,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )
    assert metrics.total_tokens == 15

    with pytest.raises(ValidationError):
        AdversarialAgentMetrics(
            trace_name="unknown_agent",
            model_name="fake",
            latency_ms=-1,
        )
