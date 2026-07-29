import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig

from agents.moderation.nodes.adversarial_model import (
    AdversarialAgentInvocationError,
    LLMAdversarialReviewModel,
)
from agents.moderation.nodes.dependencies import AdversarialReviewInput
from moderation.schemas import (
    ModerationAction,
    ModerationContentType,
    PolicyEvidence,
    RiskAgentResult,
    RiskClassification,
    RiskType,
    SafeAgentResult,
)
from schema.models import OpenAICompatibleName


class FakeStructuredModel:
    def __init__(self, *, delay: float = 0.0, parsing_error: bool = False) -> None:
        self.delay = delay
        self.parsing_error = parsing_error
        self.schema = None
        self.calls: list[dict] = []
        self.structured_output_calls: list[dict] = []

    def with_structured_output(self, schema, **kwargs):
        include_raw = kwargs.get("include_raw")
        assert include_raw is True
        self.schema = schema
        self.structured_output_calls.append({"schema": schema, **kwargs})
        return self

    async def ainvoke(self, messages, config):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append({"messages": messages, "config": config, "schema": self.schema})
        raw = SimpleNamespace(
            usage_metadata={
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
            },
            response_metadata={},
        )
        return {
            "parsed": _result_for(self.schema),
            "raw": raw,
            "parsing_error": ValueError("bad output") if self.parsing_error else None,
        }


def _result_for(schema):
    if schema is RiskAgentResult:
        return {
            "position": "UNCERTAIN",
            "risk_type": "PRIVACY",
            "risk_score": 0.6,
            "content_evidence": ["13812345678"],
            "uncertainties": ["Authorization is unknown."],
            "suggested_action": "HUMAN_REVIEW",
        }
    if schema is SafeAgentResult:
        return {
            "position": "UNCERTAIN",
            "false_positive_risk": 0.5,
            "missing_evidence": ["Authorization is unknown."],
            "suggested_action": "HUMAN_REVIEW",
        }
    return {
        "action": "HUMAN_REVIEW",
        "risk_type": "PRIVACY",
        "risk_score": 0.6,
        "confidence": 0.5,
        "reason": "Authorization cannot be established from the available evidence.",
        "need_human_review": True,
    }


def _review_input() -> AdversarialReviewInput:
    return AdversarialReviewInput(
        content="Contact 13812345678 for details.",
        content_hash="safe-content-hash",
        content_type=ModerationContentType.TEXT,
        classification=RiskClassification(
            risk_type=RiskType.PRIVACY,
            risk_score=0.6,
            confidence=0.72,
            indicators=["phone number"],
        ),
        policies=(
            PolicyEvidence(
                policy_id=uuid4(),
                code="PRIVACY-001",
                title="Private information",
                excerpt="Private phone numbers require authorization.",
                score=0.9,
                risk_type=RiskType.PRIVACY,
                default_action=ModerationAction.REJECT,
                version=4,
            ),
        ),
        cases=(),
        context=None,
        signals=(),
    )


@pytest.mark.asyncio
async def test_model_uses_independent_trace_names_and_safe_metadata(monkeypatch) -> None:
    fake_model = FakeStructuredModel()
    monkeypatch.setattr(
        "agents.moderation.nodes.adversarial_model.get_model",
        lambda _name: fake_model,
    )
    model = LLMAdversarialReviewModel(timeout_seconds=1)
    config = RunnableConfig(
        configurable={
            "model": OpenAICompatibleName.OPENAI_COMPATIBLE,
            "moderation_task_id": "task-123",
        },
        metadata={"untrusted": "13812345678 alice@example.com"},
        run_name="content-moderation",
    )

    risk = await model.investigate(review_input=_review_input(), config=config)
    safe = await model.advocate(review_input=_review_input(), config=config)
    judge = await model.decide_adversarial(
        review_input=_review_input(),
        risk_result=risk.result,
        safe_result=safe.result,
        agent_conflict=False,
        agent_errors=(),
        config=config,
    )

    assert [call["config"]["run_name"] for call in fake_model.calls] == [
        "risk_investigator",
        "safe_advocate",
        "adversarial_judge",
    ]
    assert risk.metrics.total_tokens == 18
    assert safe.metrics.trace_name == "safe_advocate"
    assert judge.metrics.trace_name == "adversarial_judge"
    assert [call["method"] for call in fake_model.structured_output_calls] == [
        "function_calling",
        "function_calling",
        "function_calling",
    ]
    metadata = fake_model.calls[0]["config"]["metadata"]
    assert metadata["moderation_task_id"] == "task-123"
    assert metadata["policy_versions"] == {"PRIVACY-001": 4}
    assert "13812345678" not in str(metadata)
    assert "alice@example.com" not in str(metadata)
    assert "untrusted" not in metadata


@pytest.mark.asyncio
async def test_model_timeout_is_wrapped_without_provider_message(monkeypatch) -> None:
    fake_model = FakeStructuredModel(delay=0.05)
    monkeypatch.setattr(
        "agents.moderation.nodes.adversarial_model.get_model",
        lambda _name: fake_model,
    )
    model = LLMAdversarialReviewModel(timeout_seconds=0.001)

    with pytest.raises(AdversarialAgentInvocationError) as exc_info:
        await model.investigate(
            review_input=_review_input(),
            config=RunnableConfig(configurable={"model": "fake"}),
        )

    assert exc_info.value.code == "risk_investigator:TimeoutError"
    assert exc_info.value.metrics.trace_name == "risk_investigator"


@pytest.mark.asyncio
async def test_structured_parse_failure_is_wrapped(monkeypatch) -> None:
    fake_model = FakeStructuredModel(parsing_error=True)
    monkeypatch.setattr(
        "agents.moderation.nodes.adversarial_model.get_model",
        lambda _name: fake_model,
    )

    with pytest.raises(AdversarialAgentInvocationError) as exc_info:
        await LLMAdversarialReviewModel(timeout_seconds=1).advocate(
            review_input=_review_input(),
            config=RunnableConfig(configurable={"model": "fake"}),
        )

    assert exc_info.value.code == "safe_advocate:ValueError"
