from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from database import DatabaseManager
from moderation.models import ModerationTask
from moderation.schemas import (
    AgentDecision,
    ModerationAction,
    ModerationTaskCreate,
    PolicyApplicability,
    PolicyEvidence,
    PolicyEvidenceSummary,
    PolicyGradeNextAction,
    PolicyGradeResult,
    PolicyItemGrade,
    PolicyQueryHistoryEntry,
    PolicyQueryPlan,
    PolicyRetrievalMode,
    PolicySeverity,
    RiskClassification,
    RiskType,
)
from moderation.services import ModerationWorkflowService


class FakePolicyRAGAuditGraph:
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
            title="Personal contact information",
            excerpt="Unauthorized third-party contact disclosure is prohibited.",
            score=0.94,
            risk_type=RiskType.PRIVACY,
            default_action=ModerationAction.REJECT,
            version=3,
            severity=PolicySeverity.HIGH,
            suggested_actions=[ModerationAction.REJECT],
            enabled=True,
            effective_at=datetime.now(UTC),
        )
        plan = PolicyQueryPlan(
            risk_hypotheses=[RiskType.PRIVACY],
            queries=["Whether alice@example.com and 13812345678 are protected contact data"],
            required_conditions=["Whether third-party contact data is exposed."],
            risk_type_filters=[RiskType.PRIVACY],
            severity_filters=[PolicySeverity.HIGH],
            retrieval_mode=PolicyRetrievalMode.HYBRID,
            reason="The content exposes contact information.",
        )
        history = PolicyQueryHistoryEntry(
            retrieval_round=1,
            queries=plan.queries,
            risk_type_filters=plan.risk_type_filters,
            severity_filters=plan.severity_filters,
            retrieval_mode=plan.retrieval_mode,
            vector_result_count=1,
            keyword_result_count=1,
            retrieved_policy_ids=[policy.policy_id],
            new_policy_ids=[policy.policy_id],
        )
        item_grade = PolicyItemGrade(
            policy_id=policy.policy_id,
            relevant=True,
            applicability=PolicyApplicability.APPLICABLE,
            matched_conditions=["Third-party contact data is present."],
            supports_actions=[ModerationAction.REJECT],
            confidence=0.93,
            reason="The current Policy applies.",
        )
        grade = PolicyGradeResult(
            relevant=True,
            sufficient=True,
            item_grades=[item_grade],
            applicable_policy_ids=[policy.policy_id],
            suggested_next_action=PolicyGradeNextAction.ACCEPT,
            reason="The Policy evidence is sufficient.",
        )
        summary = PolicyEvidenceSummary(
            complete=True,
            sufficient=True,
            applicable_policies=[policy],
            retrieval_rounds=1,
            queries_used=plan.queries,
            fallback_used=False,
            reason="A current applicable privacy Policy was verified.",
        )
        classification = RiskClassification(
            risk_type=RiskType.PRIVACY,
            risk_score=0.9,
            confidence=0.92,
            indicators=["phone number"],
        )
        decision = AgentDecision(
            risk_type=RiskType.PRIVACY,
            risk_score=0.9,
            confidence=0.92,
            recommended_action=ModerationAction.REJECT,
            reason="The verified privacy Policy applies.",
            matched_policies=[policy],
            evidence_complete=True,
        )
        return {
            **input,
            "normalized_content": input["content"],
            "content_hash": "safe-hash",
            "classification": classification.model_dump(mode="json"),
            "matched_policies": [policy.model_dump(mode="json")],
            "similar_cases": [],
            "signals": [],
            "policy_query_plan": plan.model_dump(mode="json"),
            "policy_query_history": [history.model_dump(mode="json")],
            "policy_grade_result": grade.model_dump(mode="json"),
            "rejected_policies": [],
            "policy_evidence_summary": summary.model_dump(mode="json"),
            "policy_rewrite_count": 0,
            "policy_rag_budget_exceeded": False,
            "policy_rag_fallback_used": False,
            "policy_rag_errors": [],
            "agent_decision": decision.model_dump(mode="json"),
            "requires_human_review": False,
            "final_action": ModerationAction.REJECT.value,
        }


@pytest.mark.asyncio
async def test_policy_rag_audit_is_persisted_returned_logged_and_redacted(tmp_path) -> None:
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'policy-rag-audit.db'}")
    try:
        service = ModerationWorkflowService(
            database=database,
            graph=FakePolicyRAGAuditGraph(),
        )
        accepted = await service.create_task(
            ModerationTaskCreate(
                content="Contact alice@example.com or 13812345678 without permission."
            )
        )

        audit = accepted.task.policy_rag
        assert audit is not None
        assert audit.query_plan is not None
        assert audit.query_plan.queries == [
            "Whether a***@example.com and 138****5678 are protected contact data"
        ]
        assert audit.evidence_summary is not None
        assert audit.evidence_summary.sufficient is True
        assert audit.evidence_summary.applicable_policies[0].code == "PRIVACY-001"

        logs = await service.list_logs(accepted.task.id)
        agent_log = next(log for log in logs if log.event == "AGENT_DECIDED")
        summary = agent_log.details["policy_rag"]
        assert summary["retrieval_mode"] == "HYBRID"
        assert summary["retrieval_rounds"] == 1
        assert summary["sufficient"] is True
        assert summary["applicable_policy_ids"] == [
            str(audit.evidence_summary.applicable_policies[0].policy_id)
        ]
        assert "query_plan" not in summary
        assert "alice@example.com" not in str(agent_log.details)
        assert "13812345678" not in str(agent_log.details)

        async with database.session() as session:
            stored = await session.get(ModerationTask, accepted.task.id)
        assert stored is not None
        assert stored.policy_rag is not None
        stored_query = stored.policy_rag["query_plan"]["queries"][0]
        assert "a***@example.com" in stored_query
        assert "138****5678" in stored_query
    finally:
        await database.close()
