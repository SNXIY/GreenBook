from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from app.creator.retrieval.fusion import CreatorRankFusion
from app.creator.retrieval.models import (
    CreatorCorpusDocument,
    CreatorEvidence,
    CreatorEvidenceGrade,
    CreatorIndexingResult,
    CreatorIndexWriteReport,
    CreatorRerankBatch,
    CreatorRerankDocument,
    CreatorRerankReport,
    CreatorRetrievalConfig,
    CreatorRetrievalPlan,
    CreatorRetrievalRequest,
    CreatorRetrievalResult,
    CreatorRetrievalRoundAudit,
    CreatorSourceHit,
    CreatorSourceReport,
    RetrievalAvailability,
    RetrievalChannel,
    RetrievalIntent,
    RetrievalNextAction,
    RetrievalSourceStatus,
)
from app.creator.retrieval.planner import CreatorRetrievalPlanner
from app.creator.retrieval.ports import (
    CreatorDocumentAuthority,
    CreatorReranker,
    CreatorRetrievalIndex,
    CreatorRetrievalSource,
)
from app.creator.retrieval.rerank import (
    HeuristicCreatorReranker,
    rerank_provider_name,
)
from app.creator.retrieval.scoring import (
    bounded_score,
    query_sha256,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ChannelExecution:
    hits: tuple[CreatorSourceHit, ...]
    report: CreatorSourceReport
    tool_calls: int


class CreatorAgenticRetriever:
    """Plans, executes, validates, fuses, grades, and rewrites retrieval."""

    def __init__(
        self,
        *,
        sources: dict[RetrievalChannel, CreatorRetrievalSource],
        authority: CreatorDocumentAuthority | None,
        reranker: CreatorReranker | None = None,
        indexes: tuple[CreatorRetrievalIndex, ...] = (),
        config: CreatorRetrievalConfig | None = None,
    ) -> None:
        self._config = config or CreatorRetrievalConfig()
        self._planner = CreatorRetrievalPlanner(self._config)
        self._sources = dict(sources)
        self._authority = authority
        self._reranker = reranker or HeuristicCreatorReranker()
        self._indexes = indexes
        self._fusion = CreatorRankFusion(
            weights=self._config.weights,
            max_excerpt_chars=self._config.max_excerpt_chars,
        )

    async def retrieve(
        self,
        request: CreatorRetrievalRequest,
    ) -> CreatorRetrievalResult:
        all_hits: list[CreatorSourceHit] = []
        audits: list[CreatorRetrievalRoundAudit] = []
        limitations: list[str] = []
        previous_grade: CreatorEvidenceGrade | None = None
        previous_candidates: frozenset[str] = frozenset()
        final_evidence: tuple[CreatorEvidence, ...] = ()
        total_tool_calls = 0

        for retrieval_round in range(1, self._config.max_rounds + 1):
            plan = self._planner.plan(
                request,
                retrieval_round=retrieval_round,
                previous_grade=previous_grade,
            )
            if plan.intent == RetrievalIntent.SKIP:
                grade = CreatorEvidenceGrade(
                    sufficient=False,
                    quality_score=0.0,
                    evidence_count=0,
                    missing_topics=(),
                    next_action=RetrievalNextAction.RETURN_PARTIAL,
                    reason=plan.reason,
                )
                audits.append(
                    CreatorRetrievalRoundAudit(
                        retrieval_round=retrieval_round,
                        plan=plan,
                        rerank_report=CreatorRerankReport(
                            provider=rerank_provider_name(self._reranker),
                            status=RetrievalSourceStatus.DISABLED,
                        ),
                        candidate_count=0,
                        hydrated_count=0,
                        evidence_count=0,
                        grade=grade,
                    )
                )
                limitations.append(plan.reason)
                break

            executions = await asyncio.gather(
                *(
                    self._execute_channel(
                        channel,
                        plan,
                        tenant_id=request.tenant_id,
                    )
                    for channel in plan.channels
                )
            )
            source_reports = [item.report for item in executions]
            total_tool_calls += sum(item.tool_calls for item in executions)
            for execution in executions:
                all_hits.extend(
                    hit
                    for hit in execution.hits
                    if _hit_is_in_scope(hit, request, plan)
                )
                if execution.report.status in {
                    RetrievalSourceStatus.DISABLED,
                    RetrievalSourceStatus.DEGRADED,
                }:
                    limitations.append(_report_limitation(execution.report))

            candidate_ids = tuple(dict.fromkeys(hit.document_id for hit in all_hits))[
                : self._config.candidate_top_k
            ]
            documents, hydration_report, hydration_calls = await self._hydrate(
                request=request,
                plan=plan,
                document_ids=candidate_ids,
            )
            total_tool_calls += hydration_calls
            source_reports.append(hydration_report)
            if hydration_report.status in {
                RetrievalSourceStatus.DISABLED,
                RetrievalSourceStatus.DEGRADED,
            }:
                limitations.append(_report_limitation(hydration_report))

            fused = self._fusion.fuse(
                tenant_id=request.tenant_id,
                requesting_creator_id=request.creator_id,
                query=request.goal,
                documents=documents,
                hits=tuple(all_hits),
                limit=plan.candidate_top_k,
            )
            rerank_batch, rerank_report, rerank_calls = await self._rerank(
                request.goal,
                fused,
            )
            total_tool_calls += rerank_calls
            final_evidence = self._fusion.apply_reranker(
                fused,
                rerank_batch,
                limit=plan.final_top_k,
            )
            grade = self._grade(plan, final_evidence)
            current_candidates = frozenset(
                evidence.document_id for evidence in final_evidence
            )
            if (
                retrieval_round > 1
                and current_candidates == previous_candidates
                and grade.next_action == RetrievalNextAction.REWRITE
            ):
                grade = grade.model_copy(
                    update={
                        "next_action": RetrievalNextAction.RETURN_PARTIAL,
                        "reason": (
                            "Query rewrite produced no new authorized evidence; "
                            "returning a bounded partial result."
                        ),
                    }
                )
            audits.append(
                CreatorRetrievalRoundAudit(
                    retrieval_round=retrieval_round,
                    plan=plan,
                    source_reports=tuple(source_reports),
                    rerank_report=rerank_report,
                    candidate_count=len(candidate_ids),
                    hydrated_count=len(documents),
                    evidence_count=len(final_evidence),
                    grade=grade,
                )
            )
            previous_grade = grade
            previous_candidates = current_candidates
            if grade.next_action != RetrievalNextAction.REWRITE:
                break

        return CreatorRetrievalResult(
            evidence=final_evidence,
            availability=_availability(tuple(audits), final_evidence),
            rounds=tuple(audits),
            limitations=tuple(dict.fromkeys(limitations))[:20],
            tool_calls=total_tool_calls,
        )

    async def index_document(
        self,
        document: CreatorCorpusDocument,
    ) -> CreatorIndexingResult:
        if self._authority is None:
            raise RuntimeError("Creator retrieval authority is required for indexing")
        authoritative = await self._authority.upsert_document(document)
        reports: list[CreatorIndexWriteReport] = [
            CreatorIndexWriteReport(
                channel=RetrievalChannel.SQL,
                backend=self._authority.backend_name,
                succeeded=True,
            )
        ]
        results = await asyncio.gather(
            *(self._index_one(index, authoritative) for index in self._indexes)
        )
        reports.extend(results)
        return CreatorIndexingResult(
            document_id=document.document_id,
            reports=tuple(reports),
        )

    async def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> CreatorIndexingResult:
        if self._authority is None:
            raise RuntimeError("Creator retrieval authority is required for deletion")
        await self._authority.delete_document(
            tenant_id=tenant_id,
            document_id=document_id,
        )
        reports: list[CreatorIndexWriteReport] = [
            CreatorIndexWriteReport(
                channel=RetrievalChannel.SQL,
                backend=self._authority.backend_name,
                succeeded=True,
            )
        ]
        results = await asyncio.gather(
            *(
                self._delete_one(
                    index,
                    tenant_id=tenant_id,
                    document_id=document_id,
                )
                for index in self._indexes
            )
        )
        reports.extend(results)
        return CreatorIndexingResult(
            document_id=document_id,
            reports=tuple(reports),
        )

    async def _execute_channel(
        self,
        channel: RetrievalChannel,
        plan: CreatorRetrievalPlan,
        *,
        tenant_id: str,
    ) -> _ChannelExecution:
        source = self._sources.get(channel)
        if source is None:
            return _ChannelExecution(
                hits=(),
                report=CreatorSourceReport(
                    channel=channel,
                    backend="not-configured",
                    status=RetrievalSourceStatus.DISABLED,
                    detail="The planned retrieval source is not configured.",
                ),
                tool_calls=0,
            )
        started = time.perf_counter()
        results = await asyncio.gather(
            *(
                self._search_once(
                    source,
                    tenant_id=tenant_id,
                    query=query,
                    plan=plan,
                )
                for query in plan.queries
            ),
            return_exceptions=True,
        )
        hits: list[CreatorSourceHit] = []
        errors: list[BaseException] = []
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                errors.append(result)
            else:
                hits.extend(result)
        deduplicated = _deduplicate_hits(hits)
        latency_ms = int((time.perf_counter() - started) * 1_000)
        if errors:
            status = RetrievalSourceStatus.DEGRADED
            error_code = f"{channel.value}_{type(errors[0]).__name__.upper()}"
            detail = f"{len(errors)} of {len(plan.queries)} source queries failed."
            logger.warning(
                "Creator retrieval source degraded channel=%s backend=%s error=%s",
                channel.value,
                source.backend_name,
                type(errors[0]).__name__,
            )
        else:
            status = (
                RetrievalSourceStatus.AVAILABLE
                if deduplicated
                else RetrievalSourceStatus.EMPTY
            )
            error_code = None
            detail = ""
        return _ChannelExecution(
            hits=deduplicated,
            report=CreatorSourceReport(
                channel=channel,
                backend=source.backend_name,
                status=status,
                query_count=len(plan.queries),
                result_count=len(deduplicated),
                latency_ms=latency_ms,
                error_code=error_code,
                detail=detail,
            ),
            tool_calls=len(plan.queries),
        )

    async def _search_once(
        self,
        source: CreatorRetrievalSource,
        *,
        tenant_id: str,
        query: str,
        plan: CreatorRetrievalPlan,
    ) -> tuple[CreatorSourceHit, ...]:
        async with asyncio.timeout(self._config.source_timeout_seconds):
            return await source.search(
                tenant_id=tenant_id,
                query=query,
                filters=plan.filters,
                limit=plan.candidate_top_k,
            )

    async def _hydrate(
        self,
        *,
        request: CreatorRetrievalRequest,
        plan: CreatorRetrievalPlan,
        document_ids: tuple[str, ...],
    ) -> tuple[
        tuple[CreatorCorpusDocument, ...],
        CreatorSourceReport,
        int,
    ]:
        authority = self._authority
        if authority is None:
            return (
                (),
                CreatorSourceReport(
                    channel=RetrievalChannel.SQL,
                    operation="HYDRATE",
                    backend="not-configured",
                    status=RetrievalSourceStatus.DISABLED,
                    detail="SQL authority is not configured.",
                ),
                0,
            )
        if not document_ids:
            return (
                (),
                CreatorSourceReport(
                    channel=RetrievalChannel.SQL,
                    operation="HYDRATE",
                    backend=authority.backend_name,
                    status=RetrievalSourceStatus.EMPTY,
                ),
                0,
            )
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._config.source_timeout_seconds):
                loaded = await authority.load_authorized(
                    tenant_id=request.tenant_id,
                    document_ids=document_ids,
                    filters=plan.filters,
                )
            documents = tuple(
                document
                for document in loaded
                if _document_is_in_scope(document, request, plan)
            )
            status = (
                RetrievalSourceStatus.AVAILABLE
                if documents
                else RetrievalSourceStatus.EMPTY
            )
            return (
                documents,
                CreatorSourceReport(
                    channel=RetrievalChannel.SQL,
                    operation="HYDRATE",
                    backend=authority.backend_name,
                    status=status,
                    query_count=1,
                    result_count=len(documents),
                    latency_ms=int((time.perf_counter() - started) * 1_000),
                ),
                1,
            )
        except Exception as exc:
            logger.warning(
                "Creator retrieval SQL hydration failed tenant_id=%s error=%s",
                request.tenant_id,
                type(exc).__name__,
            )
            return (
                (),
                CreatorSourceReport(
                    channel=RetrievalChannel.SQL,
                    operation="HYDRATE",
                    backend=authority.backend_name,
                    status=RetrievalSourceStatus.DEGRADED,
                    query_count=1,
                    latency_ms=int((time.perf_counter() - started) * 1_000),
                    error_code=f"SQL_HYDRATE_{type(exc).__name__.upper()}",
                    detail="Authoritative document hydration failed.",
                ),
                1,
            )

    async def _rerank(
        self,
        query: str,
        evidence: tuple[CreatorEvidence, ...],
    ) -> tuple[CreatorRerankBatch, CreatorRerankReport, int]:
        documents = tuple(
            CreatorRerankDocument(
                document_id=item.document_id,
                title=item.title,
                excerpt=item.excerpt,
                fused_score=item.score.fused,
            )
            for item in evidence
        )
        if not documents:
            empty = CreatorRerankBatch(
                scores={},
                provider=rerank_provider_name(self._reranker),
            )
            return (
                empty,
                CreatorRerankReport(
                    provider=empty.provider,
                    status=RetrievalSourceStatus.EMPTY,
                ),
                0,
            )
        try:
            async with asyncio.timeout(self._config.source_timeout_seconds):
                batch = await self._reranker.rerank(
                    query=query,
                    documents=documents,
                )
        except Exception as exc:
            logger.warning(
                "Creator reranker failed; applying local fallback error=%s",
                type(exc).__name__,
            )
            batch = await HeuristicCreatorReranker().rerank(
                query=query,
                documents=documents,
            )
            batch = batch.model_copy(
                update={
                    "fallback_used": True,
                    "error_code": f"RERANKER_{type(exc).__name__.upper()}",
                }
            )
        status = (
            RetrievalSourceStatus.DEGRADED
            if batch.fallback_used
            else RetrievalSourceStatus.AVAILABLE
        )
        return (
            batch,
            CreatorRerankReport(
                provider=batch.provider,
                status=status,
                candidate_count=len(documents),
                fallback_used=batch.fallback_used,
                error_code=batch.error_code,
            ),
            1,
        )

    def _grade(
        self,
        plan: CreatorRetrievalPlan,
        evidence: tuple[CreatorEvidence, ...],
    ) -> CreatorEvidenceGrade:
        expected_hashes = tuple(query_sha256(query) for query in plan.queries)
        covered = tuple(
            query_hash
            for query_hash in expected_hashes
            if any(query_hash in item.query_hashes for item in evidence)
        )
        coverage = len(covered) / len(expected_hashes) if expected_hashes else 0.0
        ranked_scores = sorted(
            (item.score.final for item in evidence),
            reverse=True,
        )[: self._config.min_evidence]
        average = sum(ranked_scores) / len(ranked_scores) if ranked_scores else 0.0
        quality = bounded_score(average * 0.75 + coverage * 0.25)
        sufficient = (
            len(evidence) >= self._config.min_evidence
            and quality >= self._config.min_grade_score
            and coverage > 0
        )
        missing = tuple(
            query
            for query, query_hash in zip(plan.queries, expected_hashes)
            if query_hash not in covered
        )
        if not missing and not sufficient:
            missing = plan.queries
        if sufficient:
            action = RetrievalNextAction.ACCEPT
            reason = "Evidence count, relevance, and query coverage passed."
        elif plan.retrieval_round < self._config.max_rounds:
            action = RetrievalNextAction.REWRITE
            reason = (
                "Evidence is insufficient; expand channels and rewrite within "
                "the retrieval budget."
            )
        else:
            action = RetrievalNextAction.RETURN_PARTIAL
            reason = "Retrieval budget was exhausted with partial evidence."
        return CreatorEvidenceGrade(
            sufficient=sufficient,
            quality_score=quality,
            evidence_count=len(evidence),
            covered_query_hashes=covered,
            missing_topics=missing,
            next_action=action,
            reason=reason,
        )

    async def _index_one(
        self,
        index: CreatorRetrievalIndex,
        document: CreatorCorpusDocument,
    ) -> CreatorIndexWriteReport:
        try:
            await index.upsert_document(document)
            return CreatorIndexWriteReport(
                channel=index.channel,
                backend=index.backend_name,
                succeeded=True,
            )
        except Exception as exc:
            logger.warning(
                "Creator retrieval index write failed channel=%s document_id=%s error=%s",
                index.channel.value,
                document.document_id,
                type(exc).__name__,
            )
            return CreatorIndexWriteReport(
                channel=index.channel,
                backend=index.backend_name,
                succeeded=False,
                error_code=f"INDEX_{type(exc).__name__.upper()}",
            )

    async def _delete_one(
        self,
        index: CreatorRetrievalIndex,
        *,
        tenant_id: str,
        document_id: str,
    ) -> CreatorIndexWriteReport:
        try:
            await index.delete_document(
                tenant_id=tenant_id,
                document_id=document_id,
            )
            return CreatorIndexWriteReport(
                channel=index.channel,
                backend=index.backend_name,
                succeeded=True,
            )
        except Exception as exc:
            logger.warning(
                "Creator retrieval index delete failed channel=%s document_id=%s error=%s",
                index.channel.value,
                document_id,
                type(exc).__name__,
            )
            return CreatorIndexWriteReport(
                channel=index.channel,
                backend=index.backend_name,
                succeeded=False,
                error_code=f"DELETE_{type(exc).__name__.upper()}",
            )


def _deduplicate_hits(
    hits: list[CreatorSourceHit],
) -> tuple[CreatorSourceHit, ...]:
    best: dict[tuple[str, str, RetrievalChannel], CreatorSourceHit] = {}
    for hit in hits:
        key = (hit.document_id, hit.query_hash, hit.channel)
        existing = best.get(key)
        if existing is None or hit.raw_score > existing.raw_score:
            best[key] = hit
    return tuple(
        sorted(
            best.values(),
            key=lambda hit: (hit.raw_score, -hit.rank),
            reverse=True,
        )
    )


def _hit_is_in_scope(
    hit: CreatorSourceHit,
    request: CreatorRetrievalRequest,
    plan: CreatorRetrievalPlan,
) -> bool:
    if hit.tenant_id != request.tenant_id:
        logger.warning(
            "Discarding cross-tenant retrieval hit channel=%s document_id=%s",
            hit.channel.value,
            hit.document_id,
        )
        return False
    if plan.filters.creator_ids and hit.creator_id not in plan.filters.creator_ids:
        return False
    if plan.filters.tags and not set(hit.tags).intersection(plan.filters.tags):
        return False
    return True


def _document_is_in_scope(
    document: CreatorCorpusDocument,
    request: CreatorRetrievalRequest,
    plan: CreatorRetrievalPlan,
) -> bool:
    if document.tenant_id != request.tenant_id:
        return False
    if not document.is_public_and_published:
        return False
    if plan.filters.creator_ids and document.creator_id not in plan.filters.creator_ids:
        return False
    if (
        plan.filters.content_types
        and document.content_type not in plan.filters.content_types
    ):
        return False
    if plan.filters.tags and not set(document.tags).intersection(plan.filters.tags):
        return False
    if plan.filters.published_after is not None and (
        document.published_at is None
        or _as_utc(document.published_at) < _as_utc(plan.filters.published_after)
    ):
        return False
    if plan.filters.published_before is not None and (
        document.published_at is None
        or _as_utc(document.published_at) >= _as_utc(plan.filters.published_before)
    ):
        return False
    return True


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _report_limitation(report: CreatorSourceReport) -> str:
    return (
        f"{report.channel.value} {report.operation.lower()} is "
        f"{report.status.value.lower()}"
        + (f" ({report.error_code})." if report.error_code else ".")
    )


def _availability(
    audits: tuple[CreatorRetrievalRoundAudit, ...],
    evidence: tuple[CreatorEvidence, ...],
) -> RetrievalAvailability:
    reports = [report for audit in audits for report in audit.source_reports]
    degraded = any(
        report.status
        in {
            RetrievalSourceStatus.DISABLED,
            RetrievalSourceStatus.DEGRADED,
        }
        for report in reports
    ) or any(audit.rerank_report.fallback_used for audit in audits)
    connected = any(
        report.status
        in {
            RetrievalSourceStatus.AVAILABLE,
            RetrievalSourceStatus.EMPTY,
        }
        for report in reports
    )
    if evidence:
        return (
            RetrievalAvailability.PARTIAL
            if degraded
            else RetrievalAvailability.AVAILABLE
        )
    if connected:
        return (
            RetrievalAvailability.PARTIAL
            if degraded
            else RetrievalAvailability.AVAILABLE
        )
    return RetrievalAvailability.NOT_CONNECTED
