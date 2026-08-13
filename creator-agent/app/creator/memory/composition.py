from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from app.creator.infrastructure.database import CreatorDatabase
from app.creator.memory.embeddings import (
    HashingCreatorEmbedder,
    OpenAICompatibleCreatorEmbedder,
)
from app.creator.memory.semantic import QdrantCreatorSemanticMemoryStore
from app.creator.memory.service import CreatorMemoryService
from app.creator.memory.short_term import RedisCreatorShortTermMemoryStore

logger = logging.getLogger(__name__)


class CreatorEmbeddingSettings(Protocol):
    creator_memory_embedding_provider: str
    creator_memory_embedding_dimensions: int
    openai_base_url: str
    openai_api_key: str
    openai_embedding_model: str
    embedding_timeout_seconds: float


class CreatorMemorySettings(Protocol):
    creator_memory_enabled: bool
    creator_short_memory_enabled: bool
    creator_short_memory_required: bool
    creator_short_memory_ttl_seconds: int
    creator_long_memory_enabled: bool
    creator_semantic_memory_enabled: bool
    creator_semantic_memory_required: bool
    creator_semantic_memory_top_k: int
    creator_memory_max_excerpt_chars: int
    creator_memory_qdrant_url: str
    creator_memory_qdrant_api_key: str
    creator_memory_qdrant_collection: str
    creator_memory_embedding_provider: str
    creator_memory_embedding_dimensions: int
    creator_memory_chunk_chars: int
    creator_memory_chunk_overlap_chars: int
    creator_memory_score_threshold: float
    redis_url: str
    redis_socket_timeout_seconds: float
    openai_base_url: str
    openai_api_key: str
    openai_embedding_model: str
    embedding_timeout_seconds: float


@asynccontextmanager
async def open_creator_memory(
    *,
    settings: CreatorMemorySettings,
    database: CreatorDatabase,
) -> AsyncIterator[CreatorMemoryService]:
    if not settings.creator_memory_enabled:
        yield CreatorMemoryService(
            short_term=None,
            long_term=None,
            semantic=None,
        )
        return

    short_store: RedisCreatorShortTermMemoryStore | None = None
    semantic_store: QdrantCreatorSemanticMemoryStore | None = None
    embedder = None
    try:
        if settings.creator_short_memory_enabled:
            try:
                short_store = RedisCreatorShortTermMemoryStore(
                    redis_url=settings.creator_redis_url,
                    ttl_seconds=settings.creator_short_memory_ttl_seconds,
                    socket_timeout_seconds=settings.redis_socket_timeout_seconds,
                )
                await short_store.ping()
            except Exception:
                if short_store is not None:
                    await short_store.aclose()
                short_store = None
                if settings.creator_short_memory_required:
                    raise
                logger.warning(
                    "Creator Redis short memory unavailable; continuing degraded",
                    exc_info=True,
                )

        if settings.creator_semantic_memory_enabled:
            try:
                embedder = build_creator_embedder(settings)
                semantic_store = QdrantCreatorSemanticMemoryStore(
                    url=settings.creator_memory_qdrant_url,
                    api_key=settings.creator_memory_qdrant_api_key,
                    collection_name=settings.creator_memory_qdrant_collection,
                    embedder=embedder,
                    chunk_chars=settings.creator_memory_chunk_chars,
                    chunk_overlap_chars=(settings.creator_memory_chunk_overlap_chars),
                    score_threshold=settings.creator_memory_score_threshold,
                )
                await semantic_store.start()
            except Exception:
                if semantic_store is not None:
                    await semantic_store.aclose()
                semantic_store = None
                if settings.creator_semantic_memory_required:
                    raise
                logger.warning(
                    "Creator Qdrant semantic memory unavailable; "
                    "continuing degraded",
                    exc_info=True,
                )

        yield CreatorMemoryService(
            short_term=short_store,
            long_term=(
                database.profile_store if settings.creator_long_memory_enabled else None
            ),
            semantic=semantic_store,
            semantic_top_k=settings.creator_semantic_memory_top_k,
            max_excerpt_chars=settings.creator_memory_max_excerpt_chars,
        )
    finally:
        if semantic_store is not None:
            await semantic_store.aclose()
        if embedder is not None and hasattr(embedder, "aclose"):
            await embedder.aclose()
        if short_store is not None:
            await short_store.aclose()


def build_creator_embedder(settings: CreatorEmbeddingSettings):
    provider = settings.creator_memory_embedding_provider.strip().lower()
    if provider == "hashing":
        return HashingCreatorEmbedder(
            dimensions=settings.creator_memory_embedding_dimensions
        )
    if provider == "openai":
        return OpenAICompatibleCreatorEmbedder(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
            model=settings.openai_embedding_model,
            dimensions=settings.creator_memory_embedding_dimensions,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    raise ValueError("CREATOR_MEMORY_EMBEDDING_PROVIDER must be 'hashing' or 'openai'")
