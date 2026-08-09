from __future__ import annotations

import logging

import httpx

from app.creator.retrieval.errors import CreatorRetrievalIntegrityError
from app.creator.retrieval.models import (
    CreatorRerankBatch,
    CreatorRerankDocument,
)
from app.creator.retrieval.ports import CreatorReranker
from app.creator.retrieval.scoring import bounded_score, lexical_relevance


logger = logging.getLogger(__name__)


class HeuristicCreatorReranker:
    """Deterministic offline fallback; production may inject a model reranker."""

    provider_name = "heuristic-local"

    async def rerank(
        self,
        *,
        query: str,
        documents: tuple[CreatorRerankDocument, ...],
    ) -> CreatorRerankBatch:
        scores = {
            document.document_id: bounded_score(
                lexical_relevance(
                    query,
                    f"{document.title}\n{document.excerpt}",
                )
                * 0.75
                + document.fused_score * 0.25
            )
            for document in documents
        }
        return CreatorRerankBatch(
            scores=scores,
            provider=self.provider_name,
        )


class HttpCreatorReranker:
    """Cohere-compatible async `/rerank` adapter."""

    provider_name = "http-reranker"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("Reranker base URL is required")
        if not api_key:
            raise ValueError("Reranker API key is required")
        if not model:
            raise ValueError("Reranker model is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def rerank(
        self,
        *,
        query: str,
        documents: tuple[CreatorRerankDocument, ...],
    ) -> CreatorRerankBatch:
        if not documents:
            return CreatorRerankBatch(
                scores={},
                provider=self.provider_name,
            )
        response = await self._client.post(
            f"{self._base_url}/rerank",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "query": query,
                "documents": [
                    f"{document.title}\n{document.excerpt}" for document in documents
                ],
                "top_n": len(documents),
            },
        )
        response.raise_for_status()
        payload = response.json()
        results = payload.get("results")
        if not isinstance(results, list):
            raise CreatorRetrievalIntegrityError(
                "Reranker response does not contain results"
            )
        scores: dict[str, float] = {}
        for result in results:
            if not isinstance(result, dict):
                raise CreatorRetrievalIntegrityError("Reranker result is malformed")
            index = int(result.get("index", -1))
            if index < 0 or index >= len(documents):
                raise CreatorRetrievalIntegrityError(
                    "Reranker returned an invalid document index"
                )
            document_id = documents[index].document_id
            if document_id in scores:
                raise CreatorRetrievalIntegrityError(
                    "Reranker returned a duplicate document index"
                )
            scores[document_id] = bounded_score(
                float(result.get("relevance_score", 0.0))
            )
        if len(scores) != len(documents):
            raise CreatorRetrievalIntegrityError(
                "Reranker did not score every candidate",
                details={
                    "expected": len(documents),
                    "actual": len(scores),
                },
            )
        return CreatorRerankBatch(
            scores=scores,
            provider=f"http:{self._model}",
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ResilientCreatorReranker:
    def __init__(
        self,
        primary: CreatorReranker,
        fallback: CreatorReranker | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or HeuristicCreatorReranker()

    async def rerank(
        self,
        *,
        query: str,
        documents: tuple[CreatorRerankDocument, ...],
    ) -> CreatorRerankBatch:
        try:
            return await self._primary.rerank(
                query=query,
                documents=documents,
            )
        except Exception as exc:
            logger.warning(
                "Creator reranker failed; using deterministic fallback error=%s",
                type(exc).__name__,
            )
            batch = await self._fallback.rerank(
                query=query,
                documents=documents,
            )
            return batch.model_copy(
                update={
                    "fallback_used": True,
                    "error_code": f"RERANKER_{type(exc).__name__.upper()}",
                }
            )


def rerank_provider_name(reranker: CreatorReranker) -> str:
    return str(getattr(reranker, "provider_name", type(reranker).__name__))
