from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from moderation.schemas.enums import ModerationAction, ModerationContentType, RiskType
from moderation.schemas.evidence import CaseEvidence, PolicyEvidence

ModerationToolName = Literal[
    "get_parent_comment",
    "get_conversation_context",
    "get_author_recent_contents",
    "get_author_violation_history",
    "get_content_reports",
    "search_platform_policies",
    "search_similar_review_cases",
    "explain_obfuscated_expression",
    "detect_contact_information",
]
ToolErrorCode = Literal[
    "TIMEOUT",
    "RETRYABLE_ERROR",
    "INVALID_ARGUMENT",
    "NOT_FOUND",
    "UNAVAILABLE",
    "INTERNAL_ERROR",
    "RESULT_TRUNCATED",
    "PARALLEL_LIMIT",
    "TOOL_NOT_ALLOWED",
    "BUDGET_EXCEEDED",
]
EvidenceRecommendedPath = Literal[
    "FAST_REVIEW",
    "ADVERSARIAL_REVIEW",
    "HUMAN_REVIEW",
]


class ToolCallingConfig(BaseModel):
    enabled: bool = True
    max_rounds: int = Field(default=4, ge=1, le=10)
    max_total_calls: int = Field(default=8, ge=1, le=32)
    max_parallel_calls: int = Field(default=3, ge=1, le=8)
    tool_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    max_result_chars: int = Field(default=4000, ge=512, le=20_000)
    max_retries: int = Field(default=1, ge=0, le=3)
    agent_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)


class ToolAgentMetrics(BaseModel):
    trace_name: Literal["moderation_tool_agent"] = "moderation_tool_agent"
    model_name: str = Field(min_length=1, max_length=200)
    latency_ms: float = Field(ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)


class ToolResult[ToolDataT](BaseModel):
    success: bool
    tool_name: ModerationToolName
    data: ToolDataT | None = None
    error_code: ToolErrorCode | None = None
    error_message: str | None = Field(default=None, max_length=500)
    is_partial: bool = False
    retryable: bool = False


class ContentEvidenceItem(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)
    content_type: ModerationContentType
    author_id: str = Field(min_length=1, max_length=128)
    content: str = Field(max_length=2000)
    title: str | None = Field(default=None, max_length=500)
    audit_status: str | None = Field(default=None, max_length=64)
    created_at: datetime | None = None


class ViolationHistoryItem(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)
    risk_type: RiskType
    action: ModerationAction
    reason: str = Field(max_length=1000)
    created_at: datetime | None = None


class ReportSummaryItem(BaseModel):
    report_type: str = Field(min_length=1, max_length=64)
    reason: str = Field(max_length=1000)
    created_at: datetime | None = None


class ParentCommentData(BaseModel):
    found: bool
    comment: ContentEvidenceItem | None = None


class ConversationContextData(BaseModel):
    items: list[ContentEvidenceItem] = Field(default_factory=list, max_length=10)


class RecentContentsData(BaseModel):
    items: list[ContentEvidenceItem] = Field(default_factory=list, max_length=10)


class ViolationHistoryData(BaseModel):
    items: list[ViolationHistoryItem] = Field(default_factory=list, max_length=50)


class ContentReportsData(BaseModel):
    report_count: int = Field(ge=0)
    items: list[ReportSummaryItem] = Field(default_factory=list, max_length=20)


class PolicySearchData(BaseModel):
    policies: list[PolicyEvidence] = Field(default_factory=list, max_length=5)


class SimilarCasesData(BaseModel):
    cases: list[CaseEvidence] = Field(default_factory=list, max_length=3)


class ObfuscatedExpressionMatch(BaseModel):
    matched_text: str = Field(min_length=1, max_length=200)
    normalized_form: str = Field(min_length=1, max_length=100)
    category: Literal["CONTACT", "ADVERTISING", "ABUSE", "UNKNOWN"]
    explanation: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)
    deterministic: bool = True


class ObfuscatedExpressionData(BaseModel):
    matches: list[ObfuscatedExpressionMatch] = Field(default_factory=list, max_length=20)
    context_used: bool = False


class ContactFinding(BaseModel):
    kind: Literal[
        "PHONE",
        "EMAIL",
        "IDENTITY_NUMBER",
        "WECHAT_HINT",
        "QQ_HINT",
        "URL",
    ]
    masked_value: str = Field(min_length=1, max_length=500)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    confidence: float = Field(ge=0.0, le=1.0)


class ContactDetectionData(BaseModel):
    has_contact_information: bool
    findings: list[ContactFinding] = Field(default_factory=list, max_length=50)


class ParentCommentResult(ToolResult[ParentCommentData]):
    pass


class ConversationContextResult(ToolResult[ConversationContextData]):
    pass


class RecentContentsResult(ToolResult[RecentContentsData]):
    pass


class ViolationHistoryResult(ToolResult[ViolationHistoryData]):
    pass


class ContentReportsResult(ToolResult[ContentReportsData]):
    pass


class PolicySearchResult(ToolResult[PolicySearchData]):
    pass


class SimilarCasesResult(ToolResult[SimilarCasesData]):
    pass


class ObfuscatedExpressionResult(ToolResult[ObfuscatedExpressionData]):
    pass


class ContactDetectionResult(ToolResult[ContactDetectionData]):
    pass


class GetParentCommentInput(BaseModel):
    comment_id: str = Field(min_length=1, max_length=128)


class GetConversationContextInput(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=10)


class GetAuthorRecentContentsInput(BaseModel):
    author_id: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=10, ge=1, le=10)


class GetAuthorViolationHistoryInput(BaseModel):
    author_id: str = Field(min_length=1, max_length=128)


class GetContentReportsInput(BaseModel):
    content_id: str = Field(min_length=1, max_length=128)


class SearchPlatformPoliciesInput(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    risk_type: RiskType | None = None
    limit: int = Field(default=5, ge=1, le=5)


class SearchSimilarReviewCasesInput(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    risk_type: RiskType | None = None
    limit: int = Field(default=3, ge=1, le=3)


class ExplainObfuscatedExpressionInput(BaseModel):
    expression: str = Field(min_length=1, max_length=1000)
    context: str | None = Field(default=None, max_length=4000)


class DetectContactInformationInput(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)


class EvidenceItem(BaseModel):
    source: ModerationToolName
    category: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=2000)
    quote: str | None = Field(default=None, max_length=1000)
    policy_id: str | None = Field(default=None, max_length=128)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class EvidenceCollectionResult(BaseModel):
    complete: bool
    risk_hypotheses: list[RiskType] = Field(default_factory=list, max_length=4)
    collected_evidence: list[EvidenceItem] = Field(default_factory=list, max_length=50)
    missing_evidence: list[str] = Field(default_factory=list, max_length=20)
    used_tools: list[ModerationToolName] = Field(default_factory=list, max_length=9)
    failed_tools: list[ModerationToolName] = Field(default_factory=list, max_length=9)
    recommended_path: EvidenceRecommendedPath
    reason: str = Field(min_length=1, max_length=2000)


class ToolExecutionRecord(BaseModel):
    tool_name: ModerationToolName
    success: bool
    cache_hit: bool = False
    round: int = Field(ge=1)
    error_code: ToolErrorCode | None = None
    is_partial: bool = False


class EvidenceCollectionAudit(BaseModel):
    dynamic_attempted: bool = True
    fallback_used: bool = False
    complete: bool = False
    risk_hypotheses: list[RiskType] = Field(default_factory=list, max_length=4)
    collected_evidence: list[EvidenceItem] = Field(default_factory=list, max_length=50)
    missing_evidence: list[str] = Field(default_factory=list, max_length=20)
    called_tools: list[ModerationToolName] = Field(default_factory=list, max_length=9)
    failed_tools: list[ModerationToolName] = Field(default_factory=list, max_length=9)
    tool_executions: list[ToolExecutionRecord] = Field(default_factory=list, max_length=32)
    tool_call_count: int = Field(default=0, ge=0, le=32)
    tool_call_round: int = Field(default=0, ge=0, le=10)
    cache_hits: int = Field(default=0, ge=0)
    budget_exceeded: bool = False
    recommended_path: EvidenceRecommendedPath
    reason: str = Field(min_length=1, max_length=2000)
    tool_agent_error: str | None = Field(default=None, max_length=500)
    tool_agent_metrics: ToolAgentMetrics | None = None


def evidence_collection_audit_from_state(
    state: Mapping[str, Any],
) -> EvidenceCollectionAudit | None:
    dynamic_attempted = bool(
        state.get("use_dynamic_tool_agent")
        or state.get("tool_agent_fallback_used")
        or state.get("tool_call_round")
    )
    if not dynamic_attempted:
        return None

    summary = state.get("evidence_summary")
    if not isinstance(summary, Mapping):
        return None
    collection_value = summary.get("collection_result")
    try:
        collection = EvidenceCollectionResult.model_validate(collection_value)
    except (TypeError, ValueError):
        return None

    executions: list[ToolExecutionRecord] = []
    for value in state.get("tool_results", []):
        if not isinstance(value, Mapping):
            continue
        try:
            executions.append(
                ToolExecutionRecord.model_validate(
                    {
                        "tool_name": value.get("tool_name"),
                        "success": value.get("success", False),
                        "cache_hit": value.get("cache_hit", False),
                        "round": value.get("round", 1),
                        "error_code": value.get("error_code"),
                        "is_partial": value.get("is_partial", False),
                    }
                )
            )
        except (TypeError, ValueError):
            continue

    metrics = None
    metrics_value = state.get("tool_agent_metrics")
    if isinstance(metrics_value, Mapping) and metrics_value.get("model_name"):
        try:
            metrics = ToolAgentMetrics.model_validate(metrics_value)
        except (TypeError, ValueError):
            metrics = None

    return EvidenceCollectionAudit(
        fallback_used=bool(state.get("tool_agent_fallback_used", False)),
        complete=bool(state.get("evidence_collection_complete", collection.complete)),
        risk_hypotheses=collection.risk_hypotheses,
        collected_evidence=collection.collected_evidence,
        missing_evidence=collection.missing_evidence,
        called_tools=collection.used_tools,
        failed_tools=collection.failed_tools,
        tool_executions=executions[:32],
        tool_call_count=int(state.get("tool_call_count") or 0),
        tool_call_round=int(state.get("tool_call_round") or 0),
        cache_hits=int(state.get("tool_cache_hits") or 0),
        budget_exceeded=bool(state.get("tool_budget_exceeded", False)),
        recommended_path=collection.recommended_path,
        reason=collection.reason,
        tool_agent_error=(
            str(state["tool_agent_error"]) if state.get("tool_agent_error") else None
        ),
        tool_agent_metrics=metrics,
    )
