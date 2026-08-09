from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from app.creator.infrastructure.database import CreatorDatabase
from app.creator.memory.composition import build_creator_embedder
from app.creator.retrieval.models import (
    CreatorFusionWeights,
    CreatorRetrievalConfig,
    RetrievalChannel,
)
from app.creator.retrieval.ports import (
    CreatorReranker,
    CreatorRetrievalIndex,
    CreatorRetrievalSource,
)
from app.creator.retrieval.qdrant import QdrantCreatorVectorSource
from app.creator.retrieval.rerank import (
    HeuristicCreatorReranker,
    HttpCreatorReranker,
    ResilientCreatorReranker,
)
from app.creator.retrieval.service import CreatorAgenticRetriever


logger = logging.getLogger(__name__)


class CreatorRetrievalSettings(Protocol):
    creator_retrieval_enabled: bool
    creator_retrieval_sql_enabled: bool
    creator_retrieval_qdrant_enabled: bool
    creator_retrieval_qdrant_required: bool
    creator_retrieval_qdrant_url: str
    creator_retrieval_qdrant_api_key: str
    creator_retrieval_qdrant_collection: str
    creator_retrieval_chunk_chars: int
    creator_retrieval_chunk_overlap_chars: int
    creator_retrieval_score_threshold: float
    creator_retrieval_max_queries_per_round: int
    creator_retrieval_max_rounds: int
    creator_retrieval_candidate_top_k: int
    creator_retrieval_final_top_k: int
    creator_retrieval_min_evidence: int
    creator_retrieval_min_grade_score: float
    creator_retrieval_source_timeout_seconds: float
    creator_retrieval_max_excerpt_chars: int
    creator_retrieval_bm25_weight: float
    creator_retrieval_vector_weight: float
    creator_retrieval_business_weight: float
    creator_retrieval_rrf_weight: float
    creator_retrieval_freshness_weight: float
    creator_retrieval_creator_affinity_weight: float
    creator_retrieval_source_authority_weight: float
    creator_retrieval_reranker_weight: float
    creator_retrieval_reranker_provider: str
    creator_retrieval_reranker_base_url: str
    creator_retrieval_reranker_api_key: str
    creator_retrieval_reranker_model: str
    creator_memory_embedding_provider: str
    creator_memory_embedding_dimensions: int
    openai_base_url: str
    openai_api_key: str
    openai_embedding_model: str
    embedding_timeout_seconds: float


@asynccontextmanager
async def open_creator_retrieval(
    *,
    settings: CreatorRetrievalSettings,
    database: CreatorDatabase,
) -> AsyncIterator[CreatorAgenticRetriever | None]:
    if not settings.creator_retrieval_enabled:
        yield None
        return

    sources: dict[RetrievalChannel, CreatorRetrievalSource] = {}
    indexes: list[CreatorRetrievalIndex] = []
    authority = (
        database.retrieval_store if settings.creator_retrieval_sql_enabled else None
    )
    if authority is not None:
        sources[RetrievalChannel.SQL] = authority

    vector: QdrantCreatorVectorSource | None = None
    embedder = None
    http_reranker: HttpCreatorReranker | None = None
    try:
        if settings.creator_retrieval_qdrant_enabled:
            try:
                embedder = build_creator_embedder(settings)
                vector = QdrantCreatorVectorSource(
                    url=settings.creator_retrieval_qdrant_url,
                    api_key=settings.creator_retrieval_qdrant_api_key,
                    collection_name=(settings.creator_retrieval_qdrant_collection),
                    embedder=embedder,
                    chunk_chars=settings.creator_retrieval_chunk_chars,
                    chunk_overlap_chars=(
                        settings.creator_retrieval_chunk_overlap_chars
                    ),
                    score_threshold=(settings.creator_retrieval_score_threshold),
                    max_excerpt_chars=(settings.creator_retrieval_max_excerpt_chars),
                )
                await vector.start()
                sources[RetrievalChannel.QDRANT] = vector
                indexes.append(vector)
            except Exception:
                if vector is not None:
                    await vector.aclose()
                vector = None
                if settings.creator_retrieval_qdrant_required:
                    raise
                logger.warning(
                    "Creator Qdrant retrieval unavailable; continuing degraded",
                    exc_info=True,
                )

        fallback = HeuristicCreatorReranker()
        provider = settings.creator_retrieval_reranker_provider.strip().lower()
        reranker: CreatorReranker
        if provider == "heuristic":
            reranker = fallback
        elif provider == "http":
            http_reranker = HttpCreatorReranker(
                base_url=settings.creator_retrieval_reranker_base_url,
                api_key=settings.creator_retrieval_reranker_api_key,
                model=settings.creator_retrieval_reranker_model,
                timeout_seconds=settings.creator_retrieval_source_timeout_seconds,
            )
            reranker = ResilientCreatorReranker(http_reranker, fallback)
        else:
            raise ValueError(
                "CREATOR_RETRIEVAL_RERANKER_PROVIDER must be " "'heuristic' or 'http'"
            )

        yield CreatorAgenticRetriever(
            sources=sources,
            authority=authority,
            reranker=reranker,
            indexes=tuple(indexes),
            config=_retrieval_config(settings),
        )
    finally:
        if http_reranker is not None:
            await http_reranker.aclose()
        if vector is not None:
            await vector.aclose()
        if embedder is not None and hasattr(embedder, "aclose"):
            await embedder.aclose()


def _retrieval_config(
    settings: CreatorRetrievalSettings,
) -> CreatorRetrievalConfig:
    return CreatorRetrievalConfig(
        max_queries_per_round=settings.creator_retrieval_max_queries_per_round,
        max_rounds=settings.creator_retrieval_max_rounds,
        candidate_top_k=settings.creator_retrieval_candidate_top_k,
        final_top_k=settings.creator_retrieval_final_top_k,
        min_evidence=settings.creator_retrieval_min_evidence,
        min_grade_score=settings.creator_retrieval_min_grade_score,
        source_timeout_seconds=(settings.creator_retrieval_source_timeout_seconds),
        max_excerpt_chars=settings.creator_retrieval_max_excerpt_chars,
        weights=CreatorFusionWeights(
            bm25=settings.creator_retrieval_bm25_weight,
            vector=settings.creator_retrieval_vector_weight,
            business=settings.creator_retrieval_business_weight,
            reciprocal_rank=settings.creator_retrieval_rrf_weight,
            freshness=settings.creator_retrieval_freshness_weight,
            creator_affinity=(settings.creator_retrieval_creator_affinity_weight),
            source_authority=(settings.creator_retrieval_source_authority_weight),
            reranker=settings.creator_retrieval_reranker_weight,
        ),
    )
