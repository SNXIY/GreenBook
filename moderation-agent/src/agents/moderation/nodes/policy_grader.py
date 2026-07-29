import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from agents.moderation.prompts import POLICY_GRADER_SYSTEM_PROMPT, POLICY_GRADER_TASK_PROMPT
from core import get_model, settings
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    ModerationSignalEvidence,
    PolicyApplicability,
    PolicyGradeNextAction,
    PolicyGradeResult,
    PolicyItemGrade,
    PolicyQueryPlan,
    RejectedPolicy,
    RetrievedPolicy,
    RiskClassification,
)
from moderation.security import redact_data

from .dependencies import PolicyGraderCall
from .structured_output import bind_moderation_structured_output

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeterministicPolicyValidation:
    valid_policies: tuple[RetrievedPolicy, ...]
    rejected_policies: tuple[RejectedPolicy, ...]


def validate_policy_facts(
    policies: list[RetrievedPolicy],
    plan: PolicyQueryPlan,
    *,
    as_of: datetime | None = None,
) -> DeterministicPolicyValidation:
    now = _as_utc(as_of or datetime.now(UTC))
    risk_filters = set(plan.risk_type_filters or plan.risk_hypotheses)
    candidates: list[RetrievedPolicy] = []
    rejected: list[RejectedPolicy] = []
    seen_ids = set()

    for policy in policies:
        if policy.policy_id in seen_ids:
            continue
        seen_ids.add(policy.policy_id)
        reason = _deterministic_rejection_reason(policy, risk_filters=risk_filters, now=now)
        if reason is not None:
            rejected.append(_rejection(policy, stage="DETERMINISTIC", reason=reason))
        else:
            candidates.append(policy)

    selected: dict[str, RetrievedPolicy] = {}
    for policy in candidates:
        current = selected.get(policy.code)
        if current is None or _version_preference(policy) > _version_preference(current):
            if current is not None:
                rejected.append(
                    _rejection(
                        current,
                        stage="DETERMINISTIC",
                        reason="A newer active version of the same Policy was retrieved.",
                    )
                )
            selected[policy.code] = policy
        else:
            rejected.append(
                _rejection(
                    policy,
                    stage="DETERMINISTIC",
                    reason="A newer active version of the same Policy was retrieved.",
                )
            )

    return DeterministicPolicyValidation(
        valid_policies=tuple(selected.values()),
        rejected_policies=tuple(rejected),
    )


class LLMPolicyGrader:
    def __init__(
        self,
        rag_config: AgenticPolicyRAGConfig | None = None,
    ) -> None:
        self.rag_config = rag_config or settings.agentic_policy_rag_config()

    async def grade(
        self,
        *,
        content: str,
        classification: RiskClassification,
        signals: list[ModerationSignalEvidence],
        evidence_summary: dict[str, Any] | None,
        plan: PolicyQueryPlan,
        policies: list[RetrievedPolicy],
        config: RunnableConfig,
    ) -> PolicyGraderCall:
        validation = validate_policy_facts(policies, plan)
        if not validation.valid_policies:
            return _fallback_call(
                validation,
                error="policy_grader:NoValidPolicies",
            )

        model_name_value = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
        model = get_model(model_name_value)  # type: ignore[arg-type]
        try:
            runnable = bind_moderation_structured_output(
                model,
                PolicyGradeResult,
                model_name=model_name_value,
                include_raw=True,
            )
            messages = [
                SystemMessage(content=POLICY_GRADER_SYSTEM_PROMPT),
                HumanMessage(
                    content=POLICY_GRADER_TASK_PROMPT.format(
                        content=content,
                        classification=classification.model_dump_json(),
                        signals="\n".join(signal.model_dump_json() for signal in signals) or "None",
                        evidence_summary=redact_data(evidence_summary)
                        if evidence_summary
                        else "None",
                        query_plan=plan.model_dump_json(),
                        policies="\n".join(
                            policy.model_dump_json() for policy in validation.valid_policies
                        ),
                    )
                ),
            ]
            call_config = _grader_config(
                config,
                classification=classification,
                model_name=str(model_name_value or "unconfigured"),
                policies=validation.valid_policies,
            )
            async with asyncio.timeout(self.rag_config.agent_timeout_seconds):
                raw_result = await _invoke_with_one_repair(runnable, messages, call_config)
            result, semantic_rejections, errors = constrain_policy_grade_result(
                raw_result,
                plan=plan,
                policies=validation.valid_policies,
                deterministic_rejections=validation.rejected_policies,
                min_confidence=self.rag_config.grader_min_confidence,
            )
            return PolicyGraderCall(
                result=result,
                considered_policies=validation.valid_policies,
                rejected_policies=(*validation.rejected_policies, *semantic_rejections),
                errors=errors,
            )
        except Exception:
            logger.exception("Policy Grader real-model invocation failed")
            raise


async def _invoke_with_one_repair(runnable, messages, config) -> PolicyGradeResult:
    current_messages = list(messages)
    for attempt in range(2):
        response = await runnable.ainvoke(current_messages, config)
        if isinstance(response, PolicyGradeResult):
            return response
        if isinstance(response, dict) and response.get("parsing_error") is None:
            parsed = response.get("parsed")
            if parsed is not None:
                return PolicyGradeResult.model_validate(parsed)
        if attempt == 0:
            current_messages.append(
                SystemMessage(
                    content=(
                        "The previous response did not match PolicyGradeResult. "
                        "Return one corrected structured result using only supplied Policy IDs."
                    )
                )
            )
    raise ValueError("Policy Grader structured output could not be repaired")


def constrain_policy_grade_result(
    raw_result: PolicyGradeResult,
    *,
    plan: PolicyQueryPlan,
    policies: tuple[RetrievedPolicy, ...],
    deterministic_rejections: tuple[RejectedPolicy, ...] = (),
    min_confidence: float,
) -> tuple[PolicyGradeResult, tuple[RejectedPolicy, ...], tuple[str, ...]]:
    raw_result = PolicyGradeResult.model_validate(redact_data(raw_result.model_dump(mode="python")))
    policy_by_id = {policy.policy_id: policy for policy in policies}
    allowed_ids = set(policy_by_id)
    referenced_ids = {
        *raw_result.applicable_policy_ids,
        *raw_result.partial_policy_ids,
        *raw_result.rejected_policy_ids,
        *(grade.policy_id for grade in raw_result.item_grades),
    }
    unknown_count = len(referenced_ids - allowed_ids)
    raw_grades = {
        grade.policy_id: grade for grade in raw_result.item_grades if grade.policy_id in allowed_ids
    }

    grades: list[PolicyItemGrade] = []
    semantic_rejections: list[RejectedPolicy] = []
    applicable_ids = []
    partial_ids = []
    rejected_ids = [item.policy_id for item in deterministic_rejections]
    for policy in policies:
        grade = raw_grades.get(policy.policy_id) or _missing_semantic_grade(policy)
        grade = _constrain_item_grade(
            grade,
            policy=policy,
            min_confidence=min_confidence,
        )
        grades.append(grade)
        if grade.applicability == PolicyApplicability.APPLICABLE:
            applicable_ids.append(policy.policy_id)
        elif grade.applicability == PolicyApplicability.PARTIALLY_APPLICABLE:
            partial_ids.append(policy.policy_id)
        else:
            rejected_ids.append(policy.policy_id)
            semantic_rejections.append(_rejection(policy, stage="SEMANTIC", reason=grade.reason))

    covered_risks = {policy_by_id[policy_id].risk_type for policy_id in applicable_ids}
    required_risks = set(plan.risk_hypotheses or plan.risk_type_filters)
    coverage_complete = not required_risks or required_risks.issubset(covered_risks)
    sufficient = bool(
        raw_result.sufficient
        and applicable_ids
        and coverage_complete
        and not raw_result.missing_policy_topics
        and not raw_result.missing_evidence
    )
    next_action = raw_result.suggested_next_action
    if sufficient:
        next_action = PolicyGradeNextAction.ACCEPT
    elif next_action == PolicyGradeNextAction.ACCEPT:
        next_action = (
            PolicyGradeNextAction.REWRITE_QUERY if policies else PolicyGradeNextAction.HUMAN_REVIEW
        )

    errors = (f"policy_grader:UnknownPolicyId:{unknown_count}",) if unknown_count else ()
    result = PolicyGradeResult(
        relevant=any(grade.relevant for grade in grades),
        sufficient=sufficient,
        item_grades=grades,
        applicable_policy_ids=applicable_ids,
        partial_policy_ids=partial_ids,
        rejected_policy_ids=list(dict.fromkeys(rejected_ids)),
        missing_policy_topics=raw_result.missing_policy_topics,
        missing_evidence=raw_result.missing_evidence,
        suggested_next_action=next_action,
        reason=raw_result.reason,
    )
    return result, tuple(semantic_rejections), errors


def _constrain_item_grade(
    grade: PolicyItemGrade,
    *,
    policy: RetrievedPolicy,
    min_confidence: float,
) -> PolicyItemGrade:
    supported = [
        action for action in grade.supports_actions if action in set(policy.suggested_actions)
    ]
    applicability = grade.applicability
    missing = list(grade.missing_conditions)
    reason = grade.reason
    if not grade.relevant:
        applicability = PolicyApplicability.NOT_APPLICABLE
    if grade.exclusion_conditions_triggered:
        applicability = PolicyApplicability.NOT_APPLICABLE
        reason = "A supplied Policy exclusion condition is triggered by the available evidence."
    elif grade.confidence < min_confidence and applicability in {
        PolicyApplicability.APPLICABLE,
        PolicyApplicability.PARTIALLY_APPLICABLE,
    }:
        applicability = PolicyApplicability.INSUFFICIENT_EVIDENCE
        missing.append("Policy applicability confidence is below the configured threshold.")
        reason = "The semantic applicability grade is below the configured confidence threshold."
    elif applicability == PolicyApplicability.APPLICABLE and not supported:
        applicability = PolicyApplicability.INSUFFICIENT_EVIDENCE
        missing.append("No model-suggested action is supported by this Policy.")
        reason = "The Policy does not support the actions claimed by the semantic grade."
    return grade.model_copy(
        update={
            "applicability": applicability,
            "missing_conditions": list(dict.fromkeys(missing))[:20],
            "supports_actions": supported,
            "reason": reason,
        }
    )


def _missing_semantic_grade(policy: RetrievedPolicy) -> PolicyItemGrade:
    return PolicyItemGrade(
        policy_id=policy.policy_id,
        relevant=True,
        applicability=PolicyApplicability.INSUFFICIENT_EVIDENCE,
        missing_conditions=["The semantic Grader did not return a grade for this Policy."],
        confidence=0.0,
        reason="No semantic applicability grade was returned for this Policy.",
    )


def _fallback_call(
    validation: DeterministicPolicyValidation,
    *,
    error: str,
) -> PolicyGraderCall:
    grades = [
        PolicyItemGrade(
            policy_id=policy.policy_id,
            relevant=True,
            applicability=PolicyApplicability.INSUFFICIENT_EVIDENCE,
            missing_conditions=["Semantic Policy applicability grading is unavailable."],
            confidence=0.0,
            reason="Deterministic checks passed, but semantic applicability was not verified.",
        )
        for policy in validation.valid_policies
    ]
    rejected_ids = [
        *(item.policy_id for item in validation.rejected_policies),
        *(item.policy_id for item in validation.valid_policies),
    ]
    return PolicyGraderCall(
        result=PolicyGradeResult(
            relevant=bool(validation.valid_policies),
            sufficient=False,
            item_grades=grades,
            rejected_policy_ids=list(dict.fromkeys(rejected_ids)),
            missing_policy_topics=["Verified semantic Policy applicability."],
            missing_evidence=["The semantic Policy Grader did not complete successfully."],
            suggested_next_action=PolicyGradeNextAction.HUMAN_REVIEW,
            reason="Policy facts were checked deterministically, but semantic grading is unavailable.",
        ),
        considered_policies=validation.valid_policies,
        rejected_policies=(
            *validation.rejected_policies,
            *(
                _rejection(
                    policy,
                    stage="SEMANTIC",
                    reason="Semantic Policy applicability was not verified.",
                )
                for policy in validation.valid_policies
            ),
        ),
        fallback_used=True,
        errors=(error,),
    )


def _deterministic_rejection_reason(
    policy: RetrievedPolicy,
    *,
    risk_filters: set,
    now: datetime,
) -> str | None:
    if policy.fact_source != "POSTGRESQL":
        return "The Policy was not verified against the PostgreSQL fact source."
    if not policy.enabled:
        return "The Policy is disabled."
    if _as_utc(policy.effective_at) > now:
        return "The Policy is not effective yet."
    if policy.expires_at is not None and _as_utc(policy.expires_at) <= now:
        return "The Policy has expired."
    if risk_filters and policy.risk_type not in risk_filters:
        return "The Policy risk type is outside the planned risk filters."
    if not policy.suggested_actions:
        return "The Policy does not support any moderation action."
    return None


def _rejection(
    policy: RetrievedPolicy,
    *,
    stage: Literal["DETERMINISTIC", "SEMANTIC"],
    reason: str,
) -> RejectedPolicy:
    return RejectedPolicy(
        policy_id=policy.policy_id,
        code=policy.code,
        stage=stage,
        reason=reason,
        retrieval_round=policy.retrieval_round,
    )


def _version_preference(policy: RetrievedPolicy) -> tuple[int, float, float]:
    return (
        policy.version,
        _as_utc(policy.effective_at).timestamp(),
        policy.combined_score,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _grader_config(
    config: RunnableConfig,
    *,
    classification: RiskClassification,
    model_name: str,
    policies: tuple[RetrievedPolicy, ...],
) -> RunnableConfig:
    call_config = config.copy()
    call_config.pop("run_id", None)
    call_config["run_name"] = "policy_grader"
    call_config["tags"] = list(
        dict.fromkeys(
            [*config.get("tags", []), "moderation", "policy_rag", "grader", "skip_stream"]
        )
    )
    call_config["metadata"] = {
        "moderation_task_id": config.get("configurable", {}).get("moderation_task_id"),
        "initial_risk_type": classification.risk_type.value,
        "model_name": model_name,
        "candidate_policy_count": len(policies),
        "candidate_policy_ids": [str(policy.policy_id) for policy in policies],
        "retrieval_round": max(policy.retrieval_round for policy in policies),
    }
    return call_config
