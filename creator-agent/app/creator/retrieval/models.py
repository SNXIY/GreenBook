from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.domain.models import CreatorTaskKind
from app.creator.memory.models import CreatorEngagementMetrics


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RetrievalModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class RetrievalChannel(str, Enum):
    QDRANT = "QDRANT"
    SQL = "SQL"


class RetrievalIntent(str, Enum):
    SKIP = "SKIP"
    TOPIC_RESEARCH = "TOPIC_RESEARCH"
    TREND_DISCOVERY = "TREND_DISCOVERY"
    FACT_CHECK = "FACT_CHECK"
    PERFORMANCE_ANALYSIS = "PERFORMANCE_ANALYSIS"


class RetrievalSourceStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    EMPTY = "EMPTY"
    DISABLED = "DISABLED"
    DEGRADED = "DEGRADED"


class RetrievalAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    NOT_CONNECTED = "NOT_CONNECTED"


class RetrievalNextAction(str, Enum):
    ACCEPT = "ACCEPT"
    REWRITE = "REWRITE"
    RETURN_PARTIAL = "RETURN_PARTIAL"


class CreatorRetrievalFilters(RetrievalModel):
    tags: tuple[str, ...] = Field(default=(), max_length=20)
    creator_ids: tuple[str, ...] = Field(default=(), max_length=20)
    content_types: tuple[str, ...] = Field(default=(), max_length=20)
    published_after: datetime | None = None
    published_before: datetime | None = None

    @model_validator(mode="after")
    def validate_time_window(self) -> "CreatorRetrievalFilters":
        values = (*self.tags, *self.creator_ids, *self.content_types)
        if any(not value.strip() or len(value) > 128 for value in values):
            raise ValueError("Retrieval filter values must contain 1-128 characters")
        if (
            self.published_after is not None
            and self.published_before is not None
            and self.published_after >= self.published_before
        ):
            raise ValueError("published_after must be earlier than published_before")
        return self


class CreatorRetrievalRequest(RetrievalModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    task_kind: CreatorTaskKind
    goal: str = Field(min_length=1, max_length=20_000)
    constraints: dict[str, Any] = Field(default_factory=dict)
    source_scope: dict[str, Any] = Field(default_factory=dict)


class CreatorRetrievalPlan(RetrievalModel):
    retrieval_round: int = Field(ge=1, le=5)
    intent: RetrievalIntent
    queries: tuple[str, ...] = Field(default=(), max_length=3)
    channels: tuple[RetrievalChannel, ...] = Field(default=(), max_length=3)
    filters: CreatorRetrievalFilters = Field(default_factory=CreatorRetrievalFilters)
    candidate_top_k: int = Field(default=20, ge=1, le=100)
    final_top_k: int = Field(default=6, ge=1, le=20)
    require_sql_hydration: bool = True
    reason: str = Field(min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_plan(self) -> "CreatorRetrievalPlan":
        if self.intent == RetrievalIntent.SKIP:
            if self.queries or self.channels:
                raise ValueError("SKIP plan cannot contain queries or channels")
            return self
        if not self.queries:
            raise ValueError("Retrieval plan requires at least one query")
        if any(not query.strip() or len(query) > 500 for query in self.queries):
            raise ValueError("Retrieval queries must contain 1-500 characters")
        if not self.channels:
            raise ValueError("Retrieval plan requires at least one channel")
        if len(set(self.queries)) != len(self.queries):
            raise ValueError("Retrieval queries must be unique")
        if len(set(self.channels)) != len(self.channels):
            raise ValueError("Retrieval channels must be unique")
        if self.final_top_k > self.candidate_top_k:
            raise ValueError("final_top_k cannot exceed candidate_top_k")
        if not self.require_sql_hydration:
            raise ValueError("SQL authorization hydration cannot be disabled")
        return self


class CreatorCorpusDocument(RetrievalModel):
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=500_000)
    description: str = Field(default="", max_length=4_000)
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    content_type: str = Field(default="image_text", max_length=64)
    visibility: str = Field(default="public", max_length=32)
    status: str = Field(default="published", max_length=32)
    source_url: str | None = Field(default=None, max_length=2_000)
    published_at: datetime | None = None
    updated_at: datetime = Field(default_factory=utc_now)
    metrics: CreatorEngagementMetrics = Field(default_factory=CreatorEngagementMetrics)
    authority_score: float = Field(default=0.7, ge=0.0, le=1.0)
    source_system: str = Field(default="zhiguang", max_length=64)
    source_revision: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_searchable_text(self) -> "CreatorCorpusDocument":
        if not (self.body.strip() or self.description.strip()):
            raise ValueError("Creator corpus document requires body or description")
        return self

    @property
    def is_public_and_published(self) -> bool:
        return (
            self.visibility.strip().lower() == "public"
            and self.status.strip().lower() == "published"
        )


class CreatorSourceHit(RetrievalModel):
    channel: RetrievalChannel
    backend: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    excerpt: str = Field(default="", max_length=4_000)
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    source_url: str | None = Field(default=None, max_length=2_000)
    published_at: datetime | None = None
    raw_score: float = Field(ge=0.0)
    rank: int = Field(ge=1)
    query_hash: str = Field(min_length=64, max_length=64)


class CreatorScoreBreakdown(RetrievalModel):
    bm25: float = Field(default=0.0, ge=0.0, le=1.0)
    embedding_similarity: float = Field(default=0.0, ge=0.0, le=1.0)
    business: float = Field(default=0.0, ge=0.0, le=1.0)
    reciprocal_rank: float = Field(default=0.0, ge=0.0, le=1.0)
    freshness: float = Field(default=0.0, ge=0.0, le=1.0)
    creator_affinity: float = Field(default=0.0, ge=0.0, le=1.0)
    source_authority: float = Field(default=0.0, ge=0.0, le=1.0)
    fused: float = Field(default=0.0, ge=0.0, le=1.0)
    reranker: float = Field(default=0.0, ge=0.0, le=1.0)
    final: float = Field(default=0.0, ge=0.0, le=1.0)


class CreatorEvidence(RetrievalModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    excerpt: str = Field(min_length=1, max_length=4_000)
    tags: tuple[str, ...] = Field(default=(), max_length=100)
    source_url: str | None = Field(default=None, max_length=2_000)
    source_system: str = Field(min_length=1, max_length=64)
    published_at: datetime | None = None
    channels: tuple[RetrievalChannel, ...] = Field(min_length=1, max_length=3)
    query_hashes: tuple[str, ...] = Field(default=(), max_length=10)
    score: CreatorScoreBreakdown
    authority_verified: bool = True


class CreatorSourceReport(RetrievalModel):
    channel: RetrievalChannel
    operation: str = Field(default="SEARCH", min_length=1, max_length=32)
    backend: str = Field(min_length=1, max_length=128)
    status: RetrievalSourceStatus
    query_count: int = Field(default=0, ge=0)
    result_count: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=128)
    detail: str = Field(default="", max_length=500)


class CreatorRerankReport(RetrievalModel):
    provider: str = Field(min_length=1, max_length=128)
    status: RetrievalSourceStatus
    candidate_count: int = Field(default=0, ge=0)
    fallback_used: bool = False
    error_code: str | None = Field(default=None, max_length=128)


class CreatorEvidenceGrade(RetrievalModel):
    sufficient: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    evidence_count: int = Field(ge=0)
    covered_query_hashes: tuple[str, ...] = Field(default=(), max_length=10)
    missing_topics: tuple[str, ...] = Field(default=(), max_length=10)
    next_action: RetrievalNextAction
    reason: str = Field(min_length=1, max_length=2_000)


class CreatorRetrievalRoundAudit(RetrievalModel):
    retrieval_round: int = Field(ge=1, le=5)
    plan: CreatorRetrievalPlan
    source_reports: tuple[CreatorSourceReport, ...] = ()
    rerank_report: CreatorRerankReport
    candidate_count: int = Field(ge=0)
    hydrated_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)
    grade: CreatorEvidenceGrade


class CreatorRetrievalResult(RetrievalModel):
    evidence: tuple[CreatorEvidence, ...] = ()
    availability: RetrievalAvailability
    rounds: tuple[CreatorRetrievalRoundAudit, ...] = ()
    limitations: tuple[str, ...] = ()
    tool_calls: int = Field(default=0, ge=0)
    generated_at: datetime = Field(default_factory=utc_now)


class CreatorRerankDocument(RetrievalModel):
    document_id: str
    title: str
    excerpt: str
    fused_score: float = Field(ge=0.0, le=1.0)


class CreatorRerankBatch(RetrievalModel):
    scores: dict[str, float]
    provider: str = Field(min_length=1, max_length=128)
    fallback_used: bool = False
    error_code: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def validate_scores(self) -> "CreatorRerankBatch":
        if any(score < 0.0 or score > 1.0 for score in self.scores.values()):
            raise ValueError("Reranker scores must be between zero and one")
        return self


class CreatorIndexWriteReport(RetrievalModel):
    channel: RetrievalChannel
    backend: str
    succeeded: bool
    error_code: str | None = None


class CreatorIndexingResult(RetrievalModel):
    document_id: str
    reports: tuple[CreatorIndexWriteReport, ...]


class CreatorFusionWeights(RetrievalModel):
    bm25: float = Field(default=0.24, ge=0.0, le=1.0)
    vector: float = Field(default=0.22, ge=0.0, le=1.0)
    business: float = Field(default=0.16, ge=0.0, le=1.0)
    reciprocal_rank: float = Field(default=0.16, ge=0.0, le=1.0)
    freshness: float = Field(default=0.07, ge=0.0, le=1.0)
    creator_affinity: float = Field(default=0.05, ge=0.0, le=1.0)
    source_authority: float = Field(default=0.10, ge=0.0, le=1.0)
    reranker: float = Field(default=0.35, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def require_fusion_weight(self) -> "CreatorFusionWeights":
        if (
            self.bm25
            + self.vector
            + self.business
            + self.reciprocal_rank
            + self.freshness
            + self.creator_affinity
            + self.source_authority
            <= 0
        ):
            raise ValueError("At least one fusion weight must be greater than zero")
        return self


class CreatorRetrievalConfig(RetrievalModel):
    max_queries_per_round: int = Field(default=3, ge=1, le=3)
    max_rounds: int = Field(default=2, ge=1, le=5)
    candidate_top_k: int = Field(default=20, ge=1, le=100)
    final_top_k: int = Field(default=6, ge=1, le=20)
    min_evidence: int = Field(default=2, ge=1, le=20)
    min_grade_score: float = Field(default=0.35, ge=0.0, le=1.0)
    source_timeout_seconds: float = Field(default=8.0, gt=0.0, le=120.0)
    max_excerpt_chars: int = Field(default=1_200, ge=200, le=4_000)
    weights: CreatorFusionWeights = Field(default_factory=CreatorFusionWeights)

    @model_validator(mode="after")
    def validate_limits(self) -> "CreatorRetrievalConfig":
        if self.final_top_k > self.candidate_top_k:
            raise ValueError("final_top_k cannot exceed candidate_top_k")
        if self.min_evidence > self.final_top_k:
            raise ValueError("min_evidence cannot exceed final_top_k")
        return self
