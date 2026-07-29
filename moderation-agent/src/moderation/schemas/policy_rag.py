from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from moderation.schemas.enums import ModerationAction, PolicySeverity, RiskType
from moderation.schemas.evidence import PolicyEvidence

PolicyQueryText = Annotated[str, Field(min_length=1, max_length=2000)]
PolicyConditionText = Annotated[str, Field(min_length=1, max_length=1000)]
PolicyReasonText = Annotated[str, Field(min_length=1, max_length=3000)]
PolicyErrorText = Annotated[str, Field(min_length=1, max_length=1000)]


class PolicyRetrievalMode(StrEnum):
    VECTOR = "VECTOR"
    KEYWORD = "KEYWORD"
    HYBRID = "HYBRID"


class PolicyApplicability(StrEnum):
    APPLICABLE = "APPLICABLE"
    PARTIALLY_APPLICABLE = "PARTIALLY_APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class PolicyGradeNextAction(StrEnum):
    ACCEPT = "ACCEPT"
    REWRITE_QUERY = "REWRITE_QUERY"
    CHANGE_FILTERS = "CHANGE_FILTERS"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class AgenticPolicyRAGConfig(BaseModel):
    enabled: bool = True
    max_queries_per_round: int = Field(default=3, ge=1, le=3)
    max_retrieval_rounds: int = Field(default=2, ge=1, le=5)
    max_total_retrieved_policies: int = Field(default=20, ge=1, le=100)
    vector_top_k: int = Field(default=5, ge=1, le=20)
    keyword_top_k: int = Field(default=5, ge=1, le=20)
    final_top_k: int = Field(default=8, ge=1, le=20)
    vector_weight: float = Field(default=0.65, ge=0.0, le=1.0)
    keyword_weight: float = Field(default=0.35, ge=0.0, le=1.0)
    min_vector_score: float = Field(default=0.45, ge=0.0, le=1.0)
    min_combined_score: float = Field(default=0.50, ge=0.0, le=1.0)
    grader_min_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    allow_partial_policy_continue: bool = True
    fallback_to_database: bool = True
    agent_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)

    @model_validator(mode="after")
    def validate_retrieval_budget(self) -> "AgenticPolicyRAGConfig":
        if self.vector_weight + self.keyword_weight <= 0:
            raise ValueError("at least one retrieval weight must be greater than zero")
        if self.final_top_k > self.max_total_retrieved_policies:
            raise ValueError("final_top_k cannot exceed max_total_retrieved_policies")
        return self


class PolicyQueryPlan(BaseModel):
    risk_hypotheses: list[RiskType] = Field(default_factory=list, max_length=4)
    queries: list[PolicyQueryText] = Field(min_length=1, max_length=3)
    required_conditions: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    exclusion_conditions_to_check: list[PolicyConditionText] = Field(
        default_factory=list,
        max_length=20,
    )
    risk_type_filters: list[RiskType] = Field(default_factory=list, max_length=4)
    severity_filters: list[PolicySeverity] = Field(default_factory=list, max_length=4)
    retrieval_mode: PolicyRetrievalMode = PolicyRetrievalMode.HYBRID
    reason: PolicyReasonText


class RetrievedPolicy(BaseModel):
    policy_id: UUID
    code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=256)
    risk_type: RiskType
    version: int = Field(ge=1)
    severity: PolicySeverity
    description: str = Field(min_length=1, max_length=5000)
    applicability_conditions: list[PolicyConditionText] = Field(
        default_factory=list,
        max_length=20,
    )
    exclusion_conditions: list[PolicyConditionText] = Field(
        default_factory=list,
        max_length=20,
    )
    violation_examples: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    safe_examples: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    default_action: ModerationAction | None = None
    suggested_actions: list[ModerationAction] = Field(default_factory=list, max_length=4)
    enabled: bool = True
    effective_at: datetime
    expires_at: datetime | None = None
    vector_score: float | None = Field(default=None, ge=0.0, le=1.0)
    keyword_score: float | None = Field(default=None, ge=0.0, le=1.0)
    combined_score: float = Field(ge=0.0, le=1.0)
    retrieval_query: PolicyQueryText
    retrieval_round: int = Field(ge=1, le=10)
    fact_source: Literal["POSTGRESQL"] = "POSTGRESQL"

    @model_validator(mode="after")
    def validate_effective_window(self) -> "RetrievedPolicy":
        if self.expires_at is not None and self.expires_at <= self.effective_at:
            raise ValueError("expires_at must be later than effective_at")
        return self


class PolicyItemGrade(BaseModel):
    policy_id: UUID
    relevant: bool
    applicability: PolicyApplicability
    matched_conditions: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    missing_conditions: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    exclusion_conditions_triggered: list[PolicyConditionText] = Field(
        default_factory=list,
        max_length=20,
    )
    supports_actions: list[ModerationAction] = Field(default_factory=list, max_length=4)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: PolicyReasonText


class RejectedPolicy(BaseModel):
    policy_id: UUID
    code: str = Field(min_length=1, max_length=64)
    stage: Literal["DETERMINISTIC", "SEMANTIC"]
    reason: PolicyReasonText
    retrieval_round: int = Field(ge=1, le=10)


class PolicyGradeResult(BaseModel):
    relevant: bool
    sufficient: bool
    item_grades: list[PolicyItemGrade] = Field(default_factory=list, max_length=20)
    applicable_policy_ids: list[UUID] = Field(default_factory=list, max_length=20)
    partial_policy_ids: list[UUID] = Field(default_factory=list, max_length=20)
    rejected_policy_ids: list[UUID] = Field(default_factory=list, max_length=20)
    missing_policy_topics: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    missing_evidence: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    suggested_next_action: PolicyGradeNextAction
    reason: PolicyReasonText


class RewrittenPolicyQuery(BaseModel):
    queries: list[PolicyQueryText] = Field(min_length=1, max_length=3)
    risk_type_filters: list[RiskType] = Field(default_factory=list, max_length=4)
    severity_filters: list[PolicySeverity] = Field(default_factory=list, max_length=4)
    retrieval_mode: PolicyRetrievalMode
    changes: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    reason: PolicyReasonText


class PolicyQueryHistoryEntry(BaseModel):
    retrieval_round: int = Field(ge=1, le=10)
    queries: list[PolicyQueryText] = Field(min_length=1, max_length=3)
    risk_type_filters: list[RiskType] = Field(default_factory=list, max_length=4)
    severity_filters: list[PolicySeverity] = Field(default_factory=list, max_length=4)
    retrieval_mode: PolicyRetrievalMode
    vector_result_count: int = Field(default=0, ge=0)
    keyword_result_count: int = Field(default=0, ge=0)
    retrieved_policy_ids: list[UUID] = Field(default_factory=list, max_length=100)
    new_policy_ids: list[UUID] = Field(default_factory=list, max_length=100)
    fallback_used: bool = False
    cache_hits: int = Field(default=0, ge=0)
    rewritten: bool = False


class PolicyEvidenceSummary(BaseModel):
    complete: bool
    sufficient: bool
    applicable_policies: list[PolicyEvidence] = Field(default_factory=list, max_length=20)
    partial_policies: list[PolicyEvidence] = Field(default_factory=list, max_length=20)
    missing_policy_topics: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    missing_evidence: list[PolicyConditionText] = Field(default_factory=list, max_length=20)
    retrieval_rounds: int = Field(default=0, ge=0, le=10)
    queries_used: list[PolicyQueryText] = Field(default_factory=list, max_length=20)
    fallback_used: bool = False
    reason: PolicyReasonText


class AgenticPolicyRAGAudit(BaseModel):
    query_plan: PolicyQueryPlan | None = None
    query_history: list[PolicyQueryHistoryEntry] = Field(default_factory=list, max_length=10)
    grade_result: PolicyGradeResult | None = None
    rejected_policies: list[RejectedPolicy] = Field(default_factory=list, max_length=100)
    evidence_summary: PolicyEvidenceSummary | None = None
    rewrite_count: int = Field(default=0, ge=0, le=10)
    budget_exceeded: bool = False
    fallback_used: bool = False
    errors: list[PolicyErrorText] = Field(default_factory=list, max_length=100)
    entered_human_review: bool = False


def agentic_policy_rag_audit_from_state(
    state: Mapping[str, Any],
    *,
    entered_human_review: bool,
) -> AgenticPolicyRAGAudit | None:
    tracked_keys = (
        "policy_query_plan",
        "policy_query_history",
        "policy_grade_result",
        "policy_evidence_summary",
        "policy_rag_errors",
    )
    if not any(state.get(key) for key in tracked_keys):
        return None

    return AgenticPolicyRAGAudit(
        query_plan=state.get("policy_query_plan"),
        query_history=list(state.get("policy_query_history") or []),
        grade_result=state.get("policy_grade_result"),
        rejected_policies=list(state.get("rejected_policies") or []),
        evidence_summary=state.get("policy_evidence_summary"),
        rewrite_count=int(state.get("policy_rewrite_count") or 0),
        budget_exceeded=bool(state.get("policy_rag_budget_exceeded", False)),
        fallback_used=bool(state.get("policy_rag_fallback_used", False)),
        errors=list(state.get("policy_rag_errors") or []),
        entered_human_review=entered_human_review,
    )
