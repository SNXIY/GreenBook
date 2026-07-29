from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig

from agents.moderation.graph import build_moderation_graph
from agents.moderation.nodes import ModerationDependencies
from community.tools import EmptyCommunityContextLoader
from moderation.schemas import (
    AgentDecision,
    ModerationAction,
    ModerationContentType,
    RiskClassification,
    RiskType,
)
from moderation.services.preflight import (
    ModerationPreflightService,
    PreflightConfig,
)


class CapturingModerationModel:
    def __init__(self) -> None:
        self.classify_calls = 0

    async def classify(self, **_: Any) -> RiskClassification:
        self.classify_calls += 1
        return RiskClassification(
            risk_type=RiskType.NORMAL,
            risk_score=0.05,
            confidence=0.95,
            indicators=[],
        )

    async def decide(self, **_: Any) -> AgentDecision:
        return AgentDecision(
            risk_type=RiskType.NORMAL,
            risk_score=0.05,
            confidence=0.95,
            recommended_action=ModerationAction.PASS,
            reason="Scripted judge pass.",
            evidence_complete=True,
        )


def _state(content: str, **overrides: Any) -> dict[str, Any]:
    task_id = str(uuid4())
    payload = {
        "task_id": task_id,
        "thread_id": task_id,
        "content": content,
        "content_type": "TEXT",
        "content_id": "content-1",
        "creator_id": "author-1",
        "platform": "community",
        "metadata": {},
    }
    payload.update(overrides)
    return payload


def _graph(*, l0: bool = True, l1: bool = False) -> tuple[Any, CapturingModerationModel]:
    model = CapturingModerationModel()
    config = PreflightConfig(l0_enabled=l0, l1_enabled=l1)
    dependencies = ModerationDependencies(
        classifier=model,
        judge=model,
        context_loader=EmptyCommunityContextLoader(),
        low_risk_fast_path_enabled=True,
        adaptive_cascade_enabled=True,
        policy_engine_enabled=False,
        preflight=ModerationPreflightService(config),
        preflight_config=config,
    )
    return build_moderation_graph(dependencies), model


async def _invoke(graph, state: dict[str, Any]):
    return await graph.ainvoke(
        state,
        RunnableConfig(
            configurable={
                "thread_id": state["thread_id"],
                "moderation_task_id": state["task_id"],
                "model": "fake",
            }
        ),
    )


@pytest.mark.asyncio
async def test_l0_rejects_hard_abuse_without_classifier() -> None:
    graph, model = _graph()
    result = await _invoke(graph, _state("你这个傻逼去死"))

    assert result["final_action"] == ModerationAction.REJECT
    assert result["final_risk_type"] == RiskType.ABUSE
    assert result["preflight_direct_decision"] is True
    assert result["preflight_layer"] == "L0"
    assert model.classify_calls == 0


@pytest.mark.asyncio
async def test_l0_limits_identity_number_without_classifier() -> None:
    graph, model = _graph()
    # Must end at a non-word boundary; Unicode letters break the detector's (?!\w).
    content = "请核对身份证号110101199001011234"
    result = await _invoke(graph, _state(content))

    assert result["final_action"] == ModerationAction.LIMIT
    assert result["final_risk_type"] == RiskType.PRIVACY
    assert result["preflight_layer"] == "L0"
    assert model.classify_calls == 0


@pytest.mark.asyncio
async def test_report_trigger_skips_l0_enforce() -> None:
    graph, model = _graph()
    result = await _invoke(
        graph,
        _state(
            "你这个傻逼去死",
            content_type=ModerationContentType.POST.value,
            metadata={"review_trigger": "REPORT"},
        ),
    )

    assert result.get("preflight_direct_decision") is not True
    assert model.classify_calls == 1
    assert "classification" in result


@pytest.mark.asyncio
async def test_normal_content_continues_to_classifier() -> None:
    graph, model = _graph()
    result = await _invoke(graph, _state("Spring Boot study notes for today"))

    assert result.get("preflight_direct_decision") is not True
    assert model.classify_calls == 1
    assert RiskClassification.model_validate(result["classification"]).risk_type == RiskType.NORMAL


@pytest.mark.asyncio
async def test_l1_clear_safe_pass_without_classifier(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_moderation(self, content: str) -> dict[str, Any]:
        return {
            "results": [
                {
                    "flagged": False,
                    "categories": {"hate": False, "harassment": False, "violence": False},
                    "category_scores": {
                        "hate": 0.001,
                        "harassment": 0.002,
                        "violence": 0.001,
                    },
                }
            ]
        }

    monkeypatch.setattr(
        ModerationPreflightService,
        "_call_openai_moderation",
        fake_moderation,
    )
    config = PreflightConfig(
        l0_enabled=False,
        l1_enabled=True,
        l1_api_key="sk-test",
    )
    model = CapturingModerationModel()
    dependencies = ModerationDependencies(
        classifier=model,
        judge=model,
        context_loader=EmptyCommunityContextLoader(),
        low_risk_fast_path_enabled=True,
        adaptive_cascade_enabled=True,
        policy_engine_enabled=False,
        preflight=ModerationPreflightService(config),
        preflight_config=config,
    )
    graph = build_moderation_graph(dependencies)
    result = await _invoke(graph, _state("A calm technical discussion"))

    assert result["final_action"] == ModerationAction.PASS
    assert result["preflight_layer"] == "L1"
    assert result["preflight_direct_decision"] is True
    assert model.classify_calls == 0
