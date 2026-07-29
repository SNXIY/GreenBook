from collections.abc import Sequence
from typing import Protocol

from moderation.schemas import CaseEvidence, RiskType


class CaseRetriever(Protocol):
    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 3,
    ) -> list[CaseEvidence]: ...


class EmptyCaseRetriever:
    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 3,
    ) -> list[CaseEvidence]:
        del query, platform, risk_types, limit
        return []


class DelegatingCaseRetriever:
    def __init__(self, backend: CaseRetriever | None = None) -> None:
        self._backend = backend or EmptyCaseRetriever()

    def configure(self, backend: CaseRetriever) -> None:
        self._backend = backend

    def reset(self) -> None:
        self._backend = EmptyCaseRetriever()

    async def search(
        self,
        *,
        query: str,
        platform: str,
        risk_types: Sequence[RiskType],
        limit: int = 3,
    ) -> list[CaseEvidence]:
        return await self._backend.search(
            query=query,
            platform=platform,
            risk_types=risk_types,
            limit=limit,
        )


default_case_retriever = DelegatingCaseRetriever()
