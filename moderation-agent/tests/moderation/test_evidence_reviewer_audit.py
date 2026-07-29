from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from database import DatabaseManager
from moderation.models import ModerationTask
from moderation.schemas import (
    AgentDecision,
    EvidenceReviewerDecision,
    EvidenceReviewerMetrics,
    ModerationAction,
    ModerationTaskCreate,
    ReviewerNextAction,
    ReviewerProblem,
    ReviewerProblemType,
    RiskClassification,
    RiskType,
)
from moderation.services import ModerationWorkflowService


class FakeEvidenceReviewerAuditGraph:
    async def ainvoke(
        self,
        input: dict[str, Any] | Command,
        config: RunnableConfig,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del config, kwargs
        assert isinstance(input, dict)
        classification = RiskClassification(
            risk_type=RiskType.PRIVACY,
            risk_score=0.72,
            confidence=0.7,
            indicators=["phone number"],
        )
        agent_decision = AgentDecision(
            risk_type=RiskType.PRIVACY,
            risk_score=0.72,
            confidence=0.62,
            recommended_action=ModerationAction.HUMAN_REVIEW,
            reason="Ownership and authorization of the contact data are unknown.",
        )
        reviewer_decision = EvidenceReviewerDecision(
            passed=False,
            problems=[
                ReviewerProblem(
                    problem_type=ReviewerProblemType.MISSING_CONTEXT,
                    description="Ownership of 13812345678 and alice@example.com is unknown.",
                    affected_fields=["context_evidence"],
                    severity="CRITICAL",
                )
            ],
            next_action=ReviewerNextAction.HUMAN_REVIEW,
            confidence=0.91,
            reason="The missing authorization context cannot be recovered safely.",
        )
        metrics = EvidenceReviewerMetrics(
            model_name="fake-reviewer-audit",
            latency_ms=4.5,
            input_tokens=20,
            output_tokens=8,
            total_tokens=28,
        )
        history = {
            "iteration": 1,
            "input_decision_version": 1,
            "decision": reviewer_decision.model_dump(mode="json"),
            "validated_route": ReviewerNextAction.HUMAN_REVIEW.value,
            "proposed_route": ReviewerNextAction.HUMAN_REVIEW.value,
            "revision_source": None,
            "metrics": metrics.model_dump(mode="json"),
            "created_at": datetime.now(UTC).isoformat(),
        }
        return {
            **input,
            "normalized_content": input["content"],
            "content_hash": "safe-content-hash",
            "classification": classification.model_dump(mode="json"),
            "matched_policies": [],
            "similar_cases": [],
            "signals": [],
            "agent_decision": agent_decision.model_dump(mode="json"),
            "reviewer_decision": reviewer_decision.model_dump(mode="json"),
            "reviewer_history": [history],
            "reviewer_iteration": 1,
            "reviewer_revision_count": 0,
            "reviewer_tool_revision_count": 0,
            "reviewer_policy_revision_count": 0,
            "reviewer_judgment_revision_count": 0,
            "reviewer_route": ReviewerNextAction.HUMAN_REVIEW.value,
            "reviewer_budget_exceeded": False,
            "reviewer_no_progress": False,
            "reviewer_errors": [],
            "requires_human_review": True,
            "__interrupt__": [{"kind": "moderation_human_review"}],
        }


@pytest.mark.asyncio
async def test_reviewer_audit_is_logged_aggregated_and_redacted(tmp_path) -> None:
    database = DatabaseManager()
    await database.start(f"sqlite+aiosqlite:///{tmp_path / 'reviewer-audit.db'}")
    try:
        service = ModerationWorkflowService(
            database=database,
            graph=FakeEvidenceReviewerAuditGraph(),
        )
        accepted = await service.create_task(
            ModerationTaskCreate(
                content="Contact alice@example.com or 13812345678 without authorization."
            )
        )

        audit = accepted.task.evidence_review
        assert audit is not None
        assert audit.final_route == ReviewerNextAction.HUMAN_REVIEW
        assert audit.iteration_count == 1
        assert len(audit.history) == 1
        assert audit.history[0].metrics is not None
        assert audit.history[0].metrics.total_tokens == 28
        serialized = audit.model_dump_json()
        assert "alice@example.com" not in serialized
        assert "13812345678" not in serialized

        logs = await service.list_logs(accepted.task.id)
        reviewer_log = next(log for log in logs if log.event == "EVIDENCE_REVIEWED")
        assert reviewer_log.details["iteration"] == 1
        assert reviewer_log.details["validated_route"] == "HUMAN_REVIEW"
        assert "alice@example.com" not in str(reviewer_log.details)
        agent_log = next(log for log in logs if log.event == "AGENT_DECIDED")
        assert agent_log.details["evidence_review"]["iteration_count"] == 1
        assert "history" not in agent_log.details["evidence_review"]

        async with database.session() as session:
            stored = await session.get(ModerationTask, accepted.task.id)
        assert stored is not None
        assert stored.evidence_review is not None
        assert stored.evidence_review["history"] == []
    finally:
        await database.close()
