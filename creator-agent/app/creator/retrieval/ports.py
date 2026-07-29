from __future__ import annotations

from typing import Protocol

from app.creator.retrieval.models import (
    CreatorCorpusDocument,
    CreatorIndexingResult,
    CreatorRerankBatch,
    CreatorRerankDocument,
    CreatorRetrievalFilters,
    CreatorRetrievalRequest,
    CreatorRetrievalResult,
    CreatorSourceHit,
    RetrievalChannel,
)


class CreatorRetrievalSource(Protocol):
    channel: RetrievalChannel
    backend_name: str

    async def search(
        self,
        *,
        tenant_id: str,
        query: str,
        filters: CreatorRetrievalFilters,
        limit: int,
    ) -> tuple[CreatorSourceHit, ...]: ...


class CreatorDocumentAuthority(CreatorRetrievalSource, Protocol):
    async def load_authorized(
        self,
        *,
        tenant_id: str,
        document_ids: tuple[str, ...],
        filters: CreatorRetrievalFilters,
    ) -> tuple[CreatorCorpusDocument, ...]: ...

    async def upsert_document(
        self,
        document: CreatorCorpusDocument,
    ) -> CreatorCorpusDocument: ...

    async def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> None: ...


class CreatorRetrievalIndex(Protocol):
    channel: RetrievalChannel
    backend_name: str

    async def upsert_document(self, document: CreatorCorpusDocument) -> int: ...

    async def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> None: ...


class CreatorReranker(Protocol):
    async def rerank(
        self,
        *,
        query: str,
        documents: tuple[CreatorRerankDocument, ...],
    ) -> CreatorRerankBatch: ...


class CreatorRetrievalReader(Protocol):
    async def retrieve(
        self,
        request: CreatorRetrievalRequest,
    ) -> CreatorRetrievalResult: ...


class CreatorRetrievalWriter(Protocol):
    async def index_document(
        self,
        document: CreatorCorpusDocument,
    ) -> CreatorIndexingResult: ...

    async def delete_document(
        self,
        *,
        tenant_id: str,
        document_id: str,
    ) -> CreatorIndexingResult: ...
