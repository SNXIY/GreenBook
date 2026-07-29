from moderation.models import (
    ModerationActionLog,
    ModerationPolicy,
    ModerationTask,
)
from moderation.schemas import (
    AdversarialReviewAudit,
    AgentDecision,
    AgenticPolicyRAGAudit,
    EvidenceReviewerAudit,
    HumanDecision,
    ModerationActionLogRead,
    ModerationPolicyRead,
    ModerationTaskDetail,
    ModerationTaskSummary,
)
from moderation.security import redact_data, redact_text


def task_to_summary(task: ModerationTask) -> ModerationTaskSummary:
    return ModerationTaskSummary(
        id=task.id,
        thread_id=task.thread_id,
        trace_id=task.trace_id,
        status=task.status,
        content=redact_text(task.content),
        content_type=task.content_type,
        agent_decision=(
            AgentDecision.model_validate(redact_data(task.agent_decision))
            if task.agent_decision
            else None
        ),
        final_action=task.final_action,
        final_risk_type=task.final_risk_type,
        version=task.version,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def task_to_detail(task: ModerationTask) -> ModerationTaskDetail:
    summary = task_to_summary(task)
    return ModerationTaskDetail(
        **summary.model_dump(),
        content_id=task.content_id,
        platform=task.platform,
        creator_id=task.creator_id,
        metadata=redact_data(task.task_metadata),
        normalized_content=(
            redact_text(task.normalized_content) if task.normalized_content else None
        ),
        adversarial_review=(
            AdversarialReviewAudit.model_validate(redact_data(task.adversarial_review))
            if task.adversarial_review
            else None
        ),
        policy_rag=(
            AgenticPolicyRAGAudit.model_validate(redact_data(task.policy_rag))
            if task.policy_rag
            else None
        ),
        evidence_review=(
            EvidenceReviewerAudit.model_validate(redact_data(task.evidence_review))
            if task.evidence_review
            else None
        ),
        human_decision=(
            HumanDecision.model_validate(redact_data(task.human_decision))
            if task.human_decision
            else None
        ),
        completed_at=task.completed_at,
        error_message=task.error_message,
    )


def policy_to_read(policy: ModerationPolicy) -> ModerationPolicyRead:
    return ModerationPolicyRead(
        id=policy.id,
        code=policy.code,
        title=policy.title,
        description=policy.description,
        risk_type=policy.risk_type,
        default_action=policy.default_action,
        platform=policy.platform,
        enabled=policy.enabled,
        priority=policy.priority,
        applicability_conditions=policy.applicability_conditions,
        exclusion_conditions=policy.exclusion_conditions,
        violation_examples=policy.violation_examples,
        safe_examples=policy.safe_examples,
        severity=policy.severity,
        suggested_actions=policy.suggested_actions,
        tags=policy.tags,
        effective_at=policy.effective_at,
        expires_at=policy.expires_at,
        version=policy.version,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
    )


def log_to_read(log: ModerationActionLog) -> ModerationActionLogRead:
    return ModerationActionLogRead(
        id=log.id,
        task_id=log.task_id,
        event=log.event,
        source=log.source,
        actor_id=log.actor_id,
        action=log.action,
        details=redact_data(log.details),
        created_at=log.created_at,
    )
