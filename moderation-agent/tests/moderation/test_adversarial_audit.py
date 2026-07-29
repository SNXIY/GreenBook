from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from database import DatabaseManager
from moderation.models import ModerationTask
from moderation.schemas import (
    AdversarialAgentMetrics,
    AgentDecision,
    JudgeAgentResult,
    ModerationAction,
    ModerationTaskCreate,
    PolicyEvidence,
    RiskAgentResult,
    RiskClassification,
    RiskType,
    SafeAgentResult,
)
from moderation.services import ModerationWorkflowService


class FakeAdversarialAuditGraph:
    async def ainvoke(
        self,
        input: dict[str, Any] | Command,
        config: RunnableConfig,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del config, kwargs
        assert isinstance(input, dict)
        policy = PolicyEvidence(
            policy_id=uuid4(),
            code="PRIVACY-001",
            title="Private information",
            excerpt="Private contact details require authorization.",
            score=0.93,
            risk_type=RiskType.PRIVACY,
            default_action=ModerationAction.REJECT,
            version=4,
        )
        classification = RiskClassification(
            risk_type=RiskType.PRIVACY,
            risk_score=0.66,
            confidence=0.74,
            indicators=["email address"],
        )
        risk = RiskAgentResult(
            position="LIKELY_VIOLATION",
            risk_type=RiskType.PRIVACY,
            risk_score=0.7,
            content_evidence=["alice@example.com"],
            matched_policy_ids=[str(policy.policy_id)],
            arguments=["A personal email address is exposed."],
            uncertainties=["Authorization is unknown."],
            suggested_action=ModerationAction.HUMAN_REVIEW,
        )
        safe = SafeAgentResult(
            position="UNCERTAIN",
            false_positive_risk=0.5,
            missing_evidence=["Authorization is not supplied."],
            suggested_action=ModerationAction.HUMAN_REVIEW,
        )
        judge = JudgeAgentResult(
            action=ModerationAction.HUMAN_REVIEW,
            risk_type=RiskType.PRIVACY,
            risk_score=0.66,
            confidence=0.58,
            accepted_risk_arguments=["The email address is present."],
            accepted_safe_arguments=["Authorization is unknown."],
            content_evidence=["alice@example.com"],
            matched_policy_ids=[str(policy.policy_id)],
            reason="Authorization cannot be established.",
            need_human_review=True,
        )
        decision = AgentDecision(
            risk_type=RiskType.PRIVACY,
            risk_score=0.66,
            confidence=0.58,
            recommended_action=ModerationAction.HUMAN_REVIEW,
            reason=judge.reason,
            matched_policies=[policy],
            source_evidence=["alice@example.com"],
        )
        metrics = {
            name: AdversarialAgentMetrics(
                trace_name=name,
                model_name="fake-adversarial",
                latency_ms=12.5,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
            ).model_dump(mode="json")
            for name in ("risk_investigator", "safe_advocate", "adversarial_judge")
        }
        return {
            **input,
            "normalized_content": input["content"],
            "content_hash": "safe-content-hash",
            "classification": classification.model_dump(mode="json"),
            "matched_policies": [policy.model_dump(mode="json")],
            "similar_cases": [],
            "signals": [],
            "use_adversarial_review": True,
            "evidence_conflict": False,
            "risk_agent_result": risk.model_dump(mode="json"),
            "safe_agent_result": safe.model_dump(mode="json"),
            "judge_agent_result": judge.model_dump(mode="json"),
            "agent_conflict": False,
            "adversarial_review_count": 1,
            "adversarial_errors": [],
            "risk_agent_metrics": metrics["risk_investigator"],
            "safe_agent_metrics": metrics["safe_advocate"],
            "judge_agent_metrics": metrics["adversarial_judge"],
            "agent_decision": decision.model_dump(mode="json"),
            "requires_human_review": True,
            "__interrupt__": [{"kind": "moderation_human_review"}],
        }


@pytest.mark.asyncio
async def test_adversarial_audit_is_persisted_returned_and_summarized(tmp_path) -> None:
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'adversarial-audit.db'}")
    try:
        service = ModerationWorkflowService(
            database=database,
            graph=FakeAdversarialAuditGraph(),
        )
        accepted = await service.create_task(
            ModerationTaskCreate(content="Contact alice@example.com or 13812345678")
        )

        assert accepted.requires_human_review is True
        audit = accepted.task.adversarial_review
        assert audit is not None
        assert audit.initial_classification.risk_type == RiskType.PRIVACY
        assert audit.policy_versions == {"PRIVACY-001": 4}
        assert audit.entered_human_review is True
        assert audit.risk_agent_result is not None
        assert audit.risk_agent_result.content_evidence == ["a***@example.com"]
        assert audit.risk_agent_metrics is not None
        assert audit.risk_agent_metrics.total_tokens == 15

        logs = await service.list_logs(accepted.task.id)
        agent_log = next(log for log in logs if log.event == "AGENT_DECIDED")
        summary = agent_log.details["adversarial_review"]
        assert summary["judge_action"] == "HUMAN_REVIEW"
        assert summary["entered_human_review"] is True
        assert summary["metrics"]["risk_investigator"]["total_tokens"] == 15
        assert "risk_agent_result" not in summary
        assert "alice@example.com" not in str(agent_log.details)

        async with database.session() as session:
            stored = await session.get(ModerationTask, accepted.task.id)
        assert stored is not None
        assert stored.adversarial_review is not None
        assert stored.adversarial_review["risk_agent_result"]["content_evidence"] == [
            "a***@example.com"
        ]
    finally:
        await database.close()
