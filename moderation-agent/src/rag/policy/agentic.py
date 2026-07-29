import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from moderation.models import ModerationPolicy
from moderation.schemas import (
    AgenticPolicyRAGConfig,
    PolicyEvidence,
    PolicyQueryHistoryEntry,
    PolicyQueryPlan,
    PolicyRetrievalMode,
    PolicySeverity,
    RetrievedPolicy,
    RiskType,
)
from rag.policy.text import normalize_policy_query

logger = logging.getLogger(__name__)


class PolicyDatabaseSource(Protocol):
    async def search_keywords(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        severities: Sequence[PolicySeverity] = (),
        limit: int = 5,
        as_of: datetime | None = None,
    ) -> list[Any]: ...

    async def load_active_by_ids(
        self,
        *,
        policy_ids: Sequence[UUID],
        platform: str,
        risk_types: Sequence[RiskType],
        severities: Sequence[PolicySeverity] = (),
        as_of: datetime | None = None,
    ) -> list[ModerationPolicy]: ...


class PolicyVectorSource(Protocol):
    async def search_policies(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int,
    ) -> list[PolicyEvidence]: ...


class PolicyFactsUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PolicyRetrievalBatch:
    policies: tuple[RetrievedPolicy, ...]
    history: PolicyQueryHistoryEntry
    errors: tuple[str, ...] = ()


@dataclass
class _CandidateScores:
    vector_score: float | None = None
    keyword_score: float | None = None
    vector_query: str | None = None
    keyword_query: str | None = None

    def add_vector(self, score: float, query: str) -> None:
        if self.vector_score is None or score > self.vector_score:
            self.vector_score = score
            self.vector_query = query

    def add_keyword(self, score: float, query: str) -> None:
        if self.keyword_score is None or score > self.keyword_score:
            self.keyword_score = score
            self.keyword_query = query


@dataclass(frozen=True)
class _QueryCandidates:
    query: str
    vector_results: tuple[PolicyEvidence, ...] = ()
    keyword_results: tuple[Any, ...] = ()
    errors: tuple[str, ...] = ()
    fallback_used: bool = False


class AgenticPolicyRetriever:
    def __init__(
        self,
        database: PolicyDatabaseSource,
        vector_index: PolicyVectorSource | None = None,
        config: AgenticPolicyRAGConfig | None = None,
    ) -> None:
        self.database = database
        self.vector_index = vector_index
        self.config = config or AgenticPolicyRAGConfig()

    async def retrieve(
        self,
        *,
        plan: PolicyQueryPlan,
        platform: str,
        retrieval_round: int,
    ) -> PolicyRetrievalBatch:
        queries = _unique_queries(plan.queries)[: self.config.max_queries_per_round]
        risk_types = tuple(plan.risk_type_filters or plan.risk_hypotheses or list(RiskType))
        severities = tuple(plan.severity_filters)
        candidates: dict[UUID, _CandidateScores] = {}
        errors: list[str] = []
        fallback_used = False
        vector_result_count = 0
        keyword_result_count = 0
        query_batches = await asyncio.gather(
            *(
                self._retrieve_query(
                    query=query,
                    mode=plan.retrieval_mode,
                    platform=platform,
                    risk_types=risk_types,
                    severities=severities,
                )
                for query in queries
            )
        )
        for batch in query_batches:
            errors.extend(batch.errors)
            fallback_used = fallback_used or batch.fallback_used
            for result in batch.vector_results:
                if result.score < self.config.min_vector_score:
                    continue
                candidates.setdefault(result.policy_id, _CandidateScores()).add_vector(
                    result.score,
                    batch.query,
                )
                vector_result_count += 1
            for result in batch.keyword_results:
                candidates.setdefault(
                    result.policy.id,
                    _CandidateScores(),
                ).add_keyword(result.score, batch.query)
                keyword_result_count += 1

        candidate_ids = _limit_candidate_ids(
            candidates,
            self.config.max_total_retrieved_policies,
        )
        try:
            policies = await self.database.load_active_by_ids(
                policy_ids=candidate_ids,
                platform=platform,
                risk_types=risk_types,
                severities=severities,
            )
        except Exception as exc:
            raise PolicyFactsUnavailableError("PostgreSQL policy facts are unavailable") from exc

        current_policies = _select_current_policies(policies, platform=platform)
        retrieved = [
            item
            for policy in current_policies
            if (
                item := _to_retrieved_policy(
                    policy,
                    candidates[policy.id],
                    mode=plan.retrieval_mode,
                    retrieval_round=retrieval_round,
                    risk_types=risk_types,
                    config=self.config,
                )
            )
            is not None
        ]
        retrieved.sort(key=lambda item: (item.combined_score, item.version), reverse=True)
        retrieved = retrieved[: self.config.final_top_k]
        history = PolicyQueryHistoryEntry(
            retrieval_round=retrieval_round,
            queries=queries,
            risk_type_filters=list(risk_types),
            severity_filters=list(severities),
            retrieval_mode=plan.retrieval_mode,
            vector_result_count=vector_result_count,
            keyword_result_count=keyword_result_count,
            retrieved_policy_ids=[policy.id for policy in current_policies],
            new_policy_ids=[item.policy_id for item in retrieved],
            fallback_used=fallback_used,
        )
        return PolicyRetrievalBatch(
            policies=tuple(retrieved),
            history=history,
            errors=tuple(dict.fromkeys(errors)),
        )

    async def _retrieve_query(
        self,
        *,
        query: str,
        mode: PolicyRetrievalMode,
        platform: str,
        risk_types: Sequence[RiskType],
        severities: Sequence[PolicySeverity],
    ) -> _QueryCandidates:
        use_vector = mode in {PolicyRetrievalMode.VECTOR, PolicyRetrievalMode.HYBRID}
        use_keyword = mode in {PolicyRetrievalMode.KEYWORD, PolicyRetrievalMode.HYBRID}
        errors: list[str] = []
        fallback_used = False
        vector_results: list[PolicyEvidence] = []
        keyword_results: list[Any] = []

        if use_vector and self.vector_index is None:
            errors.append("VECTOR_INDEX_NOT_CONFIGURED")
            fallback_used = True
            use_vector = False
            use_keyword = use_keyword or self.config.fallback_to_database

        if use_vector and use_keyword:
            (vector_results, vector_error), (keyword_results, keyword_error) = await asyncio.gather(
                self._search_vector(query, platform, risk_types),
                self._search_keywords(query, platform, risk_types, severities),
            )
            if vector_error:
                errors.append(vector_error)
                fallback_used = True
            if keyword_error:
                errors.append(keyword_error)
        elif use_vector:
            vector_results, vector_error = await self._search_vector(
                query,
                platform,
                risk_types,
            )
            if vector_error:
                errors.append(vector_error)
                fallback_used = True
                if self.config.fallback_to_database:
                    keyword_results, keyword_error = await self._search_keywords(
                        query,
                        platform,
                        risk_types,
                        severities,
                    )
                    if keyword_error:
                        errors.append(keyword_error)
        elif use_keyword:
            keyword_results, keyword_error = await self._search_keywords(
                query,
                platform,
                risk_types,
                severities,
            )
            if keyword_error:
                errors.append(keyword_error)

        return _QueryCandidates(
            query=query,
            vector_results=tuple(vector_results),
            keyword_results=tuple(keyword_results),
            errors=tuple(errors),
            fallback_used=fallback_used,
        )

    async def _search_vector(
        self,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
    ) -> tuple[list[PolicyEvidence], str | None]:
        vector_index = self.vector_index
        if vector_index is None:
            return [], "VECTOR_INDEX_NOT_CONFIGURED"
        try:
            return (
                await vector_index.search_policies(
                    query=query,
                    platform=platform,
                    risk_types=risk_types,
                    limit=self.config.vector_top_k,
                ),
                None,
            )
        except Exception:
            logger.exception("Qdrant policy candidate search failed")
            return [], "QDRANT_UNAVAILABLE"

    async def _search_keywords(
        self,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        severities: Sequence[PolicySeverity],
    ) -> tuple[list[Any], str | None]:
        try:
            return (
                await self.database.search_keywords(
                    query=query,
                    platform=platform,
                    risk_types=risk_types,
                    severities=severities,
                    limit=self.config.keyword_top_k,
                ),
                None,
            )
        except Exception:
            logger.exception("PostgreSQL policy keyword search failed")
            return [], "KEYWORD_RETRIEVAL_UNAVAILABLE"


class DelegatingAgenticPolicyRetriever:
    def __init__(self) -> None:
        self._backend: AgenticPolicyRetriever | None = None

    def configure(self, backend: AgenticPolicyRetriever) -> None:
        self._backend = backend

    def reset(self) -> None:
        self._backend = None

    async def retrieve(
        self,
        *,
        plan: PolicyQueryPlan,
        platform: str,
        retrieval_round: int,
    ) -> PolicyRetrievalBatch:
        if self._backend is None:
            raise PolicyFactsUnavailableError("Agentic Policy Retriever is not configured")
        return await self._backend.retrieve(
            plan=plan,
            platform=platform,
            retrieval_round=retrieval_round,
        )


default_agentic_policy_retriever = DelegatingAgenticPolicyRetriever()


def retrieved_policy_to_evidence(policy: RetrievedPolicy) -> PolicyEvidence:
    default_action = policy.default_action
    if default_action is None and policy.suggested_actions:
        default_action = policy.suggested_actions[0]
    return PolicyEvidence(
        policy_id=policy.policy_id,
        code=policy.code,
        title=policy.title,
        excerpt=policy.description,
        score=policy.combined_score,
        risk_type=policy.risk_type,
        default_action=default_action,
        version=policy.version,
        severity=policy.severity,
        suggested_actions=policy.suggested_actions,
        applicability_conditions=policy.applicability_conditions,
        exclusion_conditions=policy.exclusion_conditions,
        violation_examples=policy.violation_examples,
        safe_examples=policy.safe_examples,
        enabled=policy.enabled,
        effective_at=policy.effective_at,
        expires_at=policy.expires_at,
    )


def _unique_queries(queries: list[str]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for query in queries:
        normalized = normalize_policy_query(query)
        if normalized and normalized not in seen:
            seen.add(normalized)
            values.append(query.strip())
    return values


def _limit_candidate_ids(
    candidates: dict[UUID, _CandidateScores],
    limit: int,
) -> list[UUID]:
    ranked = sorted(
        candidates,
        key=lambda policy_id: max(
            candidates[policy_id].vector_score or 0.0,
            candidates[policy_id].keyword_score or 0.0,
        ),
        reverse=True,
    )
    return ranked[:limit]


def _select_current_policies(
    policies: list[ModerationPolicy],
    *,
    platform: str,
) -> list[ModerationPolicy]:
    selected: dict[str, ModerationPolicy] = {}
    for policy in policies:
        existing = selected.get(policy.code)
        if existing is None or _policy_preference(policy, platform) > _policy_preference(
            existing,
            platform,
        ):
            selected[policy.code] = policy
    return list(selected.values())


def _policy_preference(policy: ModerationPolicy, platform: str) -> tuple[int, int, float]:
    effective_at = policy.effective_at or policy.created_at or datetime.min.replace(tzinfo=UTC)
    if effective_at.tzinfo is None:
        effective_at = effective_at.replace(tzinfo=UTC)
    return (
        int(policy.platform == platform),
        policy.version,
        effective_at.timestamp(),
    )


def _to_retrieved_policy(
    policy: ModerationPolicy,
    scores: _CandidateScores,
    *,
    mode: PolicyRetrievalMode,
    retrieval_round: int,
    risk_types: tuple[RiskType, ...],
    config: AgenticPolicyRAGConfig,
) -> RetrievedPolicy | None:
    base_score = _combined_base_score(scores, mode=mode, config=config)
    risk_bonus = 0.05 if policy.risk_type in risk_types else 0.0
    severity_bonus = {
        PolicySeverity.CRITICAL: 0.05,
        PolicySeverity.HIGH: 0.03,
    }.get(policy.severity, 0.0)
    current_version_bonus = 0.02
    combined_score = min(1.0, base_score + risk_bonus + severity_bonus + current_version_bonus)
    if combined_score < config.min_combined_score:
        return None

    query = scores.vector_query or scores.keyword_query or "policy retrieval"
    if (scores.keyword_score or 0.0) > (scores.vector_score or 0.0):
        query = scores.keyword_query or query
    effective_at = policy.effective_at or policy.created_at or datetime.now(UTC)
    return RetrievedPolicy(
        policy_id=policy.id,
        code=policy.code,
        title=policy.title,
        risk_type=policy.risk_type,
        version=policy.version,
        severity=policy.severity or PolicySeverity.MEDIUM,
        description=policy.description,
        applicability_conditions=policy.applicability_conditions or [],
        exclusion_conditions=policy.exclusion_conditions or [],
        violation_examples=policy.violation_examples or [],
        safe_examples=policy.safe_examples or [],
        default_action=policy.default_action,
        suggested_actions=policy.suggested_actions or [policy.default_action],
        enabled=policy.enabled,
        effective_at=effective_at,
        expires_at=policy.expires_at,
        vector_score=scores.vector_score,
        keyword_score=scores.keyword_score,
        combined_score=combined_score,
        retrieval_query=query,
        retrieval_round=retrieval_round,
    )


def _combined_base_score(
    scores: _CandidateScores,
    *,
    mode: PolicyRetrievalMode,
    config: AgenticPolicyRAGConfig,
) -> float:
    if mode == PolicyRetrievalMode.VECTOR and scores.vector_score is not None:
        return scores.vector_score
    if mode == PolicyRetrievalMode.KEYWORD and scores.keyword_score is not None:
        return scores.keyword_score
    if scores.vector_score is None:
        return scores.keyword_score or 0.0
    if scores.keyword_score is None:
        return scores.vector_score
    total_weight = config.vector_weight + config.keyword_weight
    return (
        scores.vector_score * config.vector_weight + scores.keyword_score * config.keyword_weight
    ) / total_weight
