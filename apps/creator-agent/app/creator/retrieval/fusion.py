from __future__ import annotations

from collections import defaultdict

from app.creator.retrieval.models import (
    CreatorCorpusDocument,
    CreatorEvidence,
    CreatorFusionWeights,
    CreatorRerankBatch,
    CreatorScoreBreakdown,
    CreatorSourceHit,
    RetrievalChannel,
)
from app.creator.retrieval.scoring import (
    best_excerpt,
    bm25_scores,
    bounded_score,
    evidence_id,
    freshness_score,
    normalize_scores,
    raw_business_score,
    reciprocal_rank_score,
    searchable_text,
)


class CreatorRankFusion:
    def __init__(
        self,
        *,
        weights: CreatorFusionWeights,
        max_excerpt_chars: int,
    ) -> None:
        self._weights = weights
        self._max_excerpt_chars = max_excerpt_chars

    def fuse(
        self,
        *,
        tenant_id: str,
        requesting_creator_id: str,
        query: str,
        documents: tuple[CreatorCorpusDocument, ...],
        hits: tuple[CreatorSourceHit, ...],
        limit: int,
    ) -> tuple[CreatorEvidence, ...]:
        documents_by_id = {document.document_id: document for document in documents}
        hits_by_id: dict[str, list[CreatorSourceHit]] = defaultdict(list)
        for hit in hits:
            if hit.tenant_id == tenant_id and hit.document_id in documents_by_id:
                hits_by_id[hit.document_id].append(hit)

        candidate_ids = tuple(
            document_id for document_id in documents_by_id if document_id in hits_by_id
        )
        if not candidate_ids:
            return ()

        lexical_candidate_ids = {
            document_id
            for document_id in candidate_ids
            if any(
                hit.channel == RetrievalChannel.SQL
                for hit in hits_by_id[document_id]
            )
        }
        lexical_raw = bm25_scores(
            query,
            {
                document_id: searchable_text(documents_by_id[document_id])
                for document_id in lexical_candidate_ids
            },
        )
        bm25_raw = {
            document_id: lexical_raw.get(document_id, 0.0)
            for document_id in candidate_ids
        }
        vector_scores = {
            document_id: max(
                (
                    bounded_score(hit.raw_score)
                    for hit in hits_by_id[document_id]
                    if hit.channel == RetrievalChannel.QDRANT
                ),
                default=0.0,
            )
            for document_id in candidate_ids
        }
        candidate_ids = tuple(
            document_id
            for document_id in candidate_ids
            if bm25_raw[document_id] > 0 or vector_scores[document_id] > 0
        )
        if not candidate_ids:
            return ()

        business_raw = {
            document_id: raw_business_score(documents_by_id[document_id].metrics)
            for document_id in candidate_ids
        }
        rrf_raw = {
            document_id: reciprocal_rank_score(
                [hit.rank for hit in hits_by_id[document_id]]
            )
            for document_id in candidate_ids
        }
        normalized_bm25 = normalize_scores(bm25_raw)
        business_scores = normalize_scores(business_raw)
        rrf_scores = normalize_scores(rrf_raw)
        active_weights = self._active_weights(
            has_bm25=any(score > 0 for score in bm25_raw.values()),
            has_vector=any(score > 0 for score in vector_scores.values()),
        )
        total_weight = sum(active_weights.values())

        evidence: list[CreatorEvidence] = []
        for document_id in candidate_ids:
            document = documents_by_id[document_id]
            components = {
                "bm25": normalized_bm25.get(document_id, 0.0),
                "vector": vector_scores.get(document_id, 0.0),
                "business": business_scores.get(document_id, 0.0),
                "reciprocal_rank": rrf_scores.get(document_id, 0.0),
                "freshness": freshness_score(document.published_at),
                "creator_affinity": (
                    1.0 if document.creator_id == requesting_creator_id else 0.0
                ),
                "source_authority": document.authority_score,
            }
            fused = (
                sum(
                    components[name] * weight for name, weight in active_weights.items()
                )
                / total_weight
            )
            document_hits = hits_by_id[document_id]
            channels = tuple(
                sorted(
                    {hit.channel for hit in document_hits},
                    key=lambda channel: channel.value,
                )
            )
            query_hashes = tuple(
                dict.fromkeys(hit.query_hash for hit in document_hits)
            )[:10]
            score = CreatorScoreBreakdown(
                bm25=components["bm25"],
                embedding_similarity=components["vector"],
                business=components["business"],
                reciprocal_rank=components["reciprocal_rank"],
                freshness=components["freshness"],
                creator_affinity=components["creator_affinity"],
                source_authority=components["source_authority"],
                fused=bounded_score(fused),
                final=bounded_score(fused),
            )
            evidence.append(
                CreatorEvidence(
                    evidence_id=evidence_id(tenant_id, document_id),
                    document_id=document_id,
                    creator_id=document.creator_id,
                    title=document.title,
                    excerpt=best_excerpt(
                        query,
                        document,
                        max_chars=self._max_excerpt_chars,
                    ),
                    tags=document.tags,
                    source_url=document.source_url,
                    source_system=document.source_system,
                    published_at=document.published_at,
                    channels=channels,
                    query_hashes=query_hashes,
                    score=score,
                    authority_verified=True,
                )
            )
        evidence.sort(
            key=lambda item: (item.score.fused, item.document_id),
            reverse=True,
        )
        return tuple(evidence[:limit])

    def apply_reranker(
        self,
        evidence: tuple[CreatorEvidence, ...],
        batch: CreatorRerankBatch,
        *,
        limit: int,
    ) -> tuple[CreatorEvidence, ...]:
        reranker_weight = self._weights.reranker
        fused_weight = 1.0 - reranker_weight
        reranked: list[CreatorEvidence] = []
        for item in evidence:
            reranker_score = batch.scores.get(item.document_id, 0.0)
            final = bounded_score(
                item.score.fused * fused_weight + reranker_score * reranker_weight
            )
            reranked.append(
                item.model_copy(
                    update={
                        "score": item.score.model_copy(
                            update={
                                "reranker": reranker_score,
                                "final": final,
                            }
                        )
                    }
                )
            )
        reranked.sort(
            key=lambda item: (item.score.final, item.document_id),
            reverse=True,
        )
        return tuple(reranked[:limit])

    def _active_weights(
        self,
        *,
        has_bm25: bool,
        has_vector: bool,
    ) -> dict[str, float]:
        weights = {
            "business": self._weights.business,
            "reciprocal_rank": self._weights.reciprocal_rank,
            "freshness": self._weights.freshness,
            "creator_affinity": self._weights.creator_affinity,
            "source_authority": self._weights.source_authority,
        }
        if has_bm25:
            weights["bm25"] = self._weights.bm25
        if has_vector:
            weights["vector"] = self._weights.vector
        active = {name: weight for name, weight in weights.items() if weight > 0}
        return active or {"reciprocal_rank": 1.0}
