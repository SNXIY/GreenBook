from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from agents.moderation.state import ModerationState
from moderation.schemas import (
    AgentDecision,
    CaseEvidence,
    JudgeAgentResult,
    ModerationAction,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    PolicyEvidence,
    RiskAgentResult,
    RiskType,
    SafeAgentResult,
)

_RISK_POSITIONS = {"VIOLATION", "LIKELY_VIOLATION"}
_SAFE_POSITIONS = {"SAFE", "LIKELY_SAFE"}


@dataclass(frozen=True)
class JudgeDecisionMapping:
    decision: AgentDecision
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentResultValidation[ResultT]:
    result: ResultT
    errors: tuple[str, ...] = ()


def detect_agent_conflict(
    risk_result: RiskAgentResult,
    safe_result: SafeAgentResult,
) -> bool:
    return risk_result.position in _RISK_POSITIONS and safe_result.position in _SAFE_POSITIONS


def validate_risk_agent_result(
    state: ModerationState,
    result: RiskAgentResult,
) -> AgentResultValidation[RiskAgentResult]:
    errors: list[str] = []
    policies = _models(state.get("matched_policies", []), PolicyEvidence)
    valid_policy_ids = _validated_policy_ids(
        result.matched_policy_ids,
        policies,
        result.risk_type,
        "risk agent",
        errors,
    )
    content_evidence = _validated_quotes(
        result.content_evidence,
        _content_sources(state),
        "risk agent content_evidence",
        errors,
    )
    context_evidence = _validated_quotes(
        result.context_evidence,
        _context_sources(_context(state)),
        "risk agent context_evidence",
        errors,
    )
    position = result.position
    suggested_action = result.suggested_action
    if position in _RISK_POSITIONS:
        if result.risk_type == RiskType.NORMAL:
            errors.append("risk agent violation position cannot use NORMAL")
        if not content_evidence:
            errors.append("risk agent violation position requires valid content evidence")
        if not valid_policy_ids:
            errors.append("risk agent violation position requires a retrieved policy")
        if result.suggested_action == ModerationAction.PASS:
            errors.append("risk agent violation position cannot suggest PASS")
    if errors:
        position = "UNCERTAIN"
        suggested_action = ModerationAction.HUMAN_REVIEW
    validated = result.model_copy(
        update={
            "position": position,
            "content_evidence": content_evidence,
            "context_evidence": context_evidence,
            "matched_policy_ids": valid_policy_ids,
            "suggested_action": suggested_action,
        }
    )
    return AgentResultValidation(result=validated, errors=tuple(errors))


def validate_safe_agent_result(
    state: ModerationState,
    result: SafeAgentResult,
) -> AgentResultValidation[SafeAgentResult]:
    errors: list[str] = []
    counter_evidence = _validated_quotes(
        result.counter_evidence,
        _content_sources(state) + _context_sources(_context(state)),
        "safe agent counter_evidence",
        errors,
    )
    position = result.position
    suggested_action = result.suggested_action
    if position in _SAFE_POSITIONS and result.suggested_action in {
        ModerationAction.REJECT,
        ModerationAction.LIMIT,
    }:
        errors.append("safe agent safe position cannot suggest enforcement")
    if errors:
        position = "UNCERTAIN"
        suggested_action = ModerationAction.HUMAN_REVIEW
    validated = result.model_copy(
        update={
            "position": position,
            "counter_evidence": counter_evidence,
            "suggested_action": suggested_action,
        }
    )
    return AgentResultValidation(result=validated, errors=tuple(errors))


def map_judge_to_agent_decision(
    state: ModerationState,
    judge: JudgeAgentResult,
) -> JudgeDecisionMapping:
    errors: list[str] = []
    policies = _models(state.get("matched_policies", []), PolicyEvidence)
    cases = _models(state.get("similar_cases", []), CaseEvidence)
    signals = _models(state.get("signals", []), ModerationSignalEvidence)
    context = _context(state)

    policies_by_id = {str(policy.policy_id): policy for policy in policies}
    selected_policies: list[PolicyEvidence] = []
    for index, policy_id in enumerate(dict.fromkeys(judge.matched_policy_ids)):
        policy = policies_by_id.get(policy_id)
        if policy is None:
            errors.append(f"judge matched_policy_ids[{index}] is not in retrieved policies")
            continue
        if policy.risk_type is not None and policy.risk_type != judge.risk_type:
            errors.append(f"judge matched_policy_ids[{index}] does not match the final risk type")
            continue
        selected_policies.append(policy)

    valid_content_evidence = _validated_quotes(
        judge.content_evidence,
        _content_sources(state),
        "judge content_evidence",
        errors,
    )
    valid_context_evidence = _validated_quotes(
        judge.context_evidence,
        _context_sources(context),
        "judge context_evidence",
        errors,
    )

    if judge.action in {ModerationAction.REJECT, ModerationAction.LIMIT}:
        if judge.risk_type == RiskType.NORMAL:
            errors.append("an enforcement action cannot use NORMAL as its final risk type")
        if not selected_policies:
            errors.append("an enforcement action requires a retrieved policy")
        if not valid_content_evidence:
            errors.append("an enforcement action requires valid content evidence")
    elif judge.action == ModerationAction.PASS and judge.risk_type != RiskType.NORMAL:
        errors.append("PASS requires NORMAL as the final risk type")

    requires_human_review = bool(errors) or judge.need_human_review
    if judge.action == ModerationAction.HUMAN_REVIEW:
        requires_human_review = True
    recommended_action = ModerationAction.HUMAN_REVIEW if requires_human_review else judge.action
    reason = judge.reason
    if errors:
        reason = f"{reason} Structured evidence validation requires human review."

    needs_context_review = context is not None and not context.complete
    decision = AgentDecision(
        risk_type=judge.risk_type,
        risk_score=judge.risk_score,
        confidence=judge.confidence,
        recommended_action=recommended_action,
        reason=reason,
        matched_policies=selected_policies,
        similar_cases=cases,
        signals=signals,
        context_evidence=context,
        source_evidence=list(
            dict.fromkeys(
                valid_content_evidence + _labeled_context_sources(context) + valid_context_evidence
            )
        )[:20],
        needs_context_review=needs_context_review,
        evidence_complete=False,
    )
    return JudgeDecisionMapping(decision=decision, errors=tuple(errors))


def _models[ModelT: BaseModel](
    values: list[dict[str, Any]],
    model_type: type[ModelT],
) -> list[ModelT]:
    return [model_type.model_validate(value) for value in values]


def _context(state: ModerationState) -> ModerationContextEvidence | None:
    value = state.get("context_evidence")
    return ModerationContextEvidence.model_validate(value) if value else None


def _content_sources(state: ModerationState) -> list[str]:
    return [value for value in (state.get("content"), state.get("normalized_content")) if value]


def _context_sources(context: ModerationContextEvidence | None) -> list[str]:
    if context is None:
        return []
    records = [context.current, context.post, context.parent_comment]
    records.extend(context.conversation_context)
    records.extend(context.author_recent_contents)
    sources = [record.content for record in records if record is not None]
    sources.extend(record.title for record in records if record is not None and record.title)
    sources.extend(report.reason for report in context.reports)
    sources.extend(violation.reason for violation in context.author_violation_history)
    return sources


def _labeled_context_sources(context: ModerationContextEvidence | None) -> list[str]:
    if context is None:
        return []
    evidence: list[str] = []
    if context.current is not None:
        evidence.append(f"Current content: {context.current.content}")
    if context.post is not None:
        evidence.append(f"Post: {context.post.content}")
    if context.parent_comment is not None:
        evidence.append(f"Parent comment: {context.parent_comment.content}")
    evidence.extend(
        f"Conversation: {record.content}" for record in context.conversation_context[-5:]
    )
    return evidence


def _validated_quotes(
    quotes: list[str],
    sources: list[str],
    field_name: str,
    errors: list[str],
) -> list[str]:
    valid: list[str] = []
    for index, quote in enumerate(dict.fromkeys(quotes)):
        if any(quote in source for source in sources):
            valid.append(quote)
        else:
            errors.append(f"{field_name}[{index}] does not match supplied evidence")
    return valid


def _validated_policy_ids(
    policy_ids: list[str],
    policies: list[PolicyEvidence],
    risk_type: RiskType,
    source_name: str,
    errors: list[str],
) -> list[str]:
    policies_by_id = {str(policy.policy_id): policy for policy in policies}
    valid: list[str] = []
    for index, policy_id in enumerate(dict.fromkeys(policy_ids)):
        policy = policies_by_id.get(policy_id)
        if policy is None:
            errors.append(f"{source_name} matched_policy_ids[{index}] is not in retrieved policies")
        elif policy.risk_type is not None and policy.risk_type != risk_type:
            errors.append(f"{source_name} matched_policy_ids[{index}] does not match its risk type")
        else:
            valid.append(policy_id)
    return valid
