import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from moderation.security import redact_text

ReviewerText = Annotated[str, Field(min_length=1, max_length=2000)]
ReviewerFeedbackText = Annotated[str, Field(min_length=1, max_length=1000)]


class ReviewerProblemType(StrEnum):
    POLICY_NOT_APPLICABLE = "POLICY_NOT_APPLICABLE"
    POLICY_INSUFFICIENT = "POLICY_INSUFFICIENT"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    UNSUPPORTED_EVIDENCE = "UNSUPPORTED_EVIDENCE"
    OVER_INTERPRETATION = "OVER_INTERPRETATION"
    IGNORED_COUNTER_EVIDENCE = "IGNORED_COUNTER_EVIDENCE"
    HISTORY_OVERWEIGHTED = "HISTORY_OVERWEIGHTED"
    CASE_OVERWEIGHTED = "CASE_OVERWEIGHTED"
    RISK_SCORE_MISMATCH = "RISK_SCORE_MISMATCH"
    CONFIDENCE_TOO_HIGH = "CONFIDENCE_TOO_HIGH"
    ACTION_TOO_SEVERE = "ACTION_TOO_SEVERE"
    ACTION_TOO_LENIENT = "ACTION_TOO_LENIENT"
    AGENT_CONFLICT_UNRESOLVED = "AGENT_CONFLICT_UNRESOLVED"
    PARTIAL_AGENT_FAILURE = "PARTIAL_AGENT_FAILURE"
    OTHER = "OTHER"


class ReviewerNextAction(StrEnum):
    FINALIZE = "FINALIZE"
    COLLECT_MORE_EVIDENCE = "COLLECT_MORE_EVIDENCE"
    RETRIEVE_MORE_POLICY = "RETRIEVE_MORE_POLICY"
    REVISE_JUDGMENT = "REVISE_JUDGMENT"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class ReviewerProblem(BaseModel):
    problem_type: ReviewerProblemType
    description: ReviewerText
    affected_fields: list[ReviewerFeedbackText] = Field(default_factory=list, max_length=20)
    severity: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    supporting_evidence: list[ReviewerFeedbackText] = Field(
        default_factory=list,
        max_length=20,
    )


class EvidenceReviewerDecision(BaseModel):
    passed: bool
    problems: list[ReviewerProblem] = Field(default_factory=list, max_length=20)
    next_action: ReviewerNextAction
    missing_evidence: list[ReviewerFeedbackText] = Field(default_factory=list, max_length=20)
    suggested_tools: list[ReviewerFeedbackText] = Field(default_factory=list, max_length=10)
    suggested_policy_queries: list[ReviewerFeedbackText] = Field(
        default_factory=list,
        max_length=5,
    )
    judgment_revision_instructions: list[ReviewerFeedbackText] = Field(
        default_factory=list,
        max_length=20,
    )
    confidence: float = Field(ge=0.0, le=1.0)
    reason: ReviewerText

    @model_validator(mode="after")
    def validate_route_contract(self) -> "EvidenceReviewerDecision":
        if self.passed != (self.next_action == ReviewerNextAction.FINALIZE):
            raise ValueError("passed must be true if and only if next_action is FINALIZE")
        if not self.passed and not self.problems:
            raise ValueError("a failed review must include at least one problem")
        if self.next_action == ReviewerNextAction.COLLECT_MORE_EVIDENCE:
            if not self.missing_evidence or not self.suggested_tools:
                raise ValueError(
                    "COLLECT_MORE_EVIDENCE requires missing_evidence and suggested_tools"
                )
        if (
            self.next_action == ReviewerNextAction.RETRIEVE_MORE_POLICY
            and not self.suggested_policy_queries
        ):
            raise ValueError("RETRIEVE_MORE_POLICY requires suggested_policy_queries")
        if (
            self.next_action == ReviewerNextAction.REVISE_JUDGMENT
            and not self.judgment_revision_instructions
        ):
            raise ValueError("REVISE_JUDGMENT requires judgment_revision_instructions")
        return self


class EvidenceReviewerConfig(BaseModel):
    enabled: bool = True
    max_iterations: int = Field(default=2, ge=1, le=5)
    max_tool_revisions: int = Field(default=1, ge=0, le=3)
    max_policy_revisions: int = Field(default=1, ge=0, le=3)
    max_judgment_revisions: int = Field(default=2, ge=0, le=5)
    min_reviewer_confidence: float = Field(default=0.65, ge=0.0, le=1.0)
    human_review_on_budget_exceeded: bool = True
    human_review_on_reviewer_error: bool = True
    allow_deterministic_fast_path_on_error: bool = False
    agent_timeout_seconds: float = Field(default=30.0, gt=0.0, le=120.0)

    @model_validator(mode="after")
    def validate_revision_budgets(self) -> "EvidenceReviewerConfig":
        for value in (
            self.max_tool_revisions,
            self.max_policy_revisions,
            self.max_judgment_revisions,
        ):
            if value > self.max_iterations:
                raise ValueError("per-route revision budgets cannot exceed max_iterations")
        return self


class EvidenceReviewerMetrics(BaseModel):
    trace_name: Literal["evidence_reviewer"] = "evidence_reviewer"
    model_name: str = Field(min_length=1, max_length=200)
    latency_ms: float = Field(ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    repair_attempted: bool = False


class EvidenceReviewerHistoryEntry(BaseModel):
    iteration: int = Field(ge=1, le=20)
    input_decision_version: int = Field(default=0, ge=0)
    decision: EvidenceReviewerDecision
    validated_route: ReviewerNextAction
    proposed_route: ReviewerNextAction
    revision_source: ReviewerNextAction | None = None
    metrics: EvidenceReviewerMetrics | None = None
    status: Literal["SUCCEEDED", "FAILED"] = "SUCCEEDED"
    error_code: ReviewerFeedbackText | None = None
    created_at: datetime


class EvidenceReviewerAudit(BaseModel):
    reviewer_version: str = Field(default="v1", min_length=1, max_length=64)
    passed: bool
    final_route: ReviewerNextAction
    final_confidence: float = Field(ge=0.0, le=1.0)
    iteration_count: int = Field(default=0, ge=0, le=20)
    revision_count: int = Field(default=0, ge=0, le=20)
    tool_revision_count: int = Field(default=0, ge=0, le=10)
    policy_revision_count: int = Field(default=0, ge=0, le=10)
    judgment_revision_count: int = Field(default=0, ge=0, le=10)
    budget_exceeded: bool = False
    no_progress: bool = False
    errors: list[ReviewerFeedbackText] = Field(default_factory=list, max_length=100)
    entered_human_review: bool = False
    history: list[EvidenceReviewerHistoryEntry] = Field(default_factory=list, max_length=20)


class RevisionSignature(BaseModel):
    next_action: ReviewerNextAction
    normalized_problems: list[str] = Field(default_factory=list, max_length=20)
    suggested_tools: list[str] = Field(default_factory=list, max_length=10)
    suggested_policy_queries: list[str] = Field(default_factory=list, max_length=5)

    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def revision_signature_from_decision(decision: EvidenceReviewerDecision) -> RevisionSignature:
    problems = sorted(
        {
            "|".join(
                (
                    problem.problem_type.value,
                    _normalize_signature_text(problem.description),
                    ",".join(sorted(_normalize_signature_text(v) for v in problem.affected_fields)),
                )
            )
            for problem in decision.problems
        }
    )
    return RevisionSignature(
        next_action=decision.next_action,
        normalized_problems=problems,
        suggested_tools=sorted({_normalize_signature_text(v) for v in decision.suggested_tools}),
        suggested_policy_queries=sorted(
            {_normalize_signature_text(v) for v in decision.suggested_policy_queries}
        ),
    )


def evidence_reviewer_audit_from_state(
    state: Mapping[str, Any],
    *,
    entered_human_review: bool,
) -> EvidenceReviewerAudit | None:
    decision_value = state.get("reviewer_decision")
    if not decision_value and not state.get("reviewer_history"):
        return None
    try:
        decision = EvidenceReviewerDecision.model_validate(decision_value)
    except (TypeError, ValueError):
        return None

    history: list[EvidenceReviewerHistoryEntry] = []
    for value in state.get("reviewer_history", []):
        try:
            history.append(EvidenceReviewerHistoryEntry.model_validate(value))
        except (TypeError, ValueError):
            continue
    final_route = ReviewerNextAction(state.get("reviewer_route") or decision.next_action.value)
    return EvidenceReviewerAudit(
        passed=bool(decision.passed and final_route == ReviewerNextAction.FINALIZE),
        final_route=final_route,
        final_confidence=decision.confidence,
        iteration_count=int(state.get("reviewer_iteration") or len(history)),
        revision_count=int(state.get("reviewer_revision_count") or 0),
        tool_revision_count=int(state.get("reviewer_tool_revision_count") or 0),
        policy_revision_count=int(state.get("reviewer_policy_revision_count") or 0),
        judgment_revision_count=int(state.get("reviewer_judgment_revision_count") or 0),
        budget_exceeded=bool(state.get("reviewer_budget_exceeded", False)),
        no_progress=bool(state.get("reviewer_no_progress", False)),
        errors=list(dict.fromkeys(state.get("reviewer_errors") or []))[:100],
        entered_human_review=entered_human_review,
        history=history,
    )


def _normalize_signature_text(value: str) -> str:
    return re.sub(r"\s+", " ", redact_text(value)).strip().lower()
