from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from moderation.schemas.decision import RiskClassification
from moderation.schemas.enums import ModerationAction, RiskType
from moderation.schemas.evidence import PolicyEvidence

EvidenceText = Annotated[str, Field(min_length=1, max_length=2000)]
PolicyId = Annotated[str, Field(min_length=1, max_length=128)]
ArgumentText = Annotated[str, Field(min_length=1, max_length=2000)]
AdversarialTraceName = Literal[
    "risk_investigator",
    "safe_advocate",
    "adversarial_judge",
]


class RiskAgentResult(BaseModel):
    position: Literal["VIOLATION", "LIKELY_VIOLATION", "UNCERTAIN"]
    risk_type: RiskType
    risk_score: float = Field(ge=0.0, le=1.0)
    content_evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    context_evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    matched_policy_ids: list[PolicyId] = Field(default_factory=list, max_length=20)
    arguments: list[ArgumentText] = Field(default_factory=list, max_length=20)
    uncertainties: list[ArgumentText] = Field(default_factory=list, max_length=20)
    suggested_action: ModerationAction


class SafeAgentResult(BaseModel):
    position: Literal["SAFE", "LIKELY_SAFE", "UNCERTAIN"]
    false_positive_risk: float = Field(ge=0.0, le=1.0)
    alternative_interpretations: list[ArgumentText] = Field(
        default_factory=list,
        max_length=20,
    )
    counter_evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    missing_evidence: list[ArgumentText] = Field(default_factory=list, max_length=20)
    policy_applicability_issues: list[ArgumentText] = Field(
        default_factory=list,
        max_length=20,
    )
    suggested_action: ModerationAction


class JudgeAgentResult(BaseModel):
    action: ModerationAction
    risk_type: RiskType
    risk_score: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)
    accepted_risk_arguments: list[ArgumentText] = Field(
        default_factory=list,
        max_length=20,
    )
    accepted_safe_arguments: list[ArgumentText] = Field(
        default_factory=list,
        max_length=20,
    )
    rejected_arguments: list[ArgumentText] = Field(default_factory=list, max_length=20)
    content_evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    context_evidence: list[EvidenceText] = Field(default_factory=list, max_length=20)
    matched_policy_ids: list[PolicyId] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=2000)
    need_human_review: bool = False


class AdversarialAgentMetrics(BaseModel):
    trace_name: AdversarialTraceName
    model_name: str = Field(min_length=1, max_length=256)
    latency_ms: float = Field(ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class AdversarialReviewAudit(BaseModel):
    use_adversarial_review: bool = True
    initial_classification: RiskClassification
    policy_versions: dict[str, int] = Field(default_factory=dict)
    evidence_conflict: bool = False
    risk_agent_result: RiskAgentResult | None = None
    safe_agent_result: SafeAgentResult | None = None
    judge_agent_result: JudgeAgentResult | None = None
    agent_conflict: bool = False
    adversarial_review_count: int = Field(default=0, ge=0)
    adversarial_errors: list[ArgumentText] = Field(default_factory=list, max_length=100)
    risk_agent_metrics: AdversarialAgentMetrics | None = None
    safe_agent_metrics: AdversarialAgentMetrics | None = None
    judge_agent_metrics: AdversarialAgentMetrics | None = None
    entered_human_review: bool = False


def adversarial_review_audit_from_state(
    state: Mapping[str, Any],
    *,
    entered_human_review: bool,
) -> AdversarialReviewAudit | None:
    has_adversarial_result = any(
        state.get(key) is not None
        for key in ("risk_agent_result", "safe_agent_result", "judge_agent_result")
    )
    if not state.get("use_adversarial_review", False) and not has_adversarial_result:
        return None
    classification = state.get("classification")
    if classification is None:
        return None

    policy_versions: dict[str, int] = {}
    for value in state.get("matched_policies", []):
        try:
            policy = PolicyEvidence.model_validate(value)
        except (TypeError, ValueError):
            continue
        if policy.version is not None:
            policy_versions[policy.code] = policy.version

    return AdversarialReviewAudit(
        initial_classification=RiskClassification.model_validate(classification),
        policy_versions=policy_versions,
        evidence_conflict=bool(state.get("evidence_conflict", False)),
        risk_agent_result=state.get("risk_agent_result"),
        safe_agent_result=state.get("safe_agent_result"),
        judge_agent_result=state.get("judge_agent_result"),
        agent_conflict=bool(state.get("agent_conflict", False)),
        adversarial_review_count=int(state.get("adversarial_review_count") or 0),
        adversarial_errors=list(state.get("adversarial_errors") or []),
        risk_agent_metrics=state.get("risk_agent_metrics"),
        safe_agent_metrics=state.get("safe_agent_metrics"),
        judge_agent_metrics=state.get("judge_agent_metrics"),
        entered_human_review=entered_human_review,
    )
