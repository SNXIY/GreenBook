import logging
from collections.abc import Awaitable
from typing import Protocol, TypeVar, runtime_checkable

from community.providers.base import CommunityDataProvider
from moderation.schemas import (
    CommunityContentRecord,
    CommunityContentSnapshot,
    ModerationContentType,
    ModerationContextEvidence,
    ReportEvidence,
    ViolationRecord,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")


class CommunityContextLoader(Protocol):
    async def load_context(
        self,
        *,
        content_id: str,
        content_type: ModerationContentType,
        author_id: str | None,
    ) -> ModerationContextEvidence | None: ...


@runtime_checkable
class CommunityEvidenceReader(Protocol):
    async def get_parent_comment(
        self,
        content_id: str,
    ) -> CommunityContentRecord | None: ...

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]: ...

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]: ...

    async def get_author_violation_history(
        self,
        author_id: str,
    ) -> list[ViolationRecord]: ...

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]: ...


class EmptyCommunityContextLoader:
    async def load_context(
        self,
        *,
        content_id: str,
        content_type: ModerationContentType,
        author_id: str | None,
    ) -> ModerationContextEvidence | None:
        del content_id, content_type, author_id
        return None

    async def get_parent_comment(
        self,
        content_id: str,
    ) -> CommunityContentRecord | None:
        del content_id
        return None

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        del content_id, limit
        return []

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        del author_id, limit
        return []

    async def get_author_violation_history(
        self,
        author_id: str,
    ) -> list[ViolationRecord]:
        del author_id
        return []

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]:
        del content_id
        return []


class CommunityContextToolset:
    """Read-only Agent tools backed by a replaceable community provider."""

    def __init__(self, provider: CommunityDataProvider) -> None:
        self.provider = provider

    async def get_content_context(self, content_id: str) -> CommunityContentSnapshot:
        return await self.provider.get_content_context(content_id)

    async def get_parent_comment(
        self,
        content_id: str,
    ) -> CommunityContentRecord | None:
        return await self.provider.get_parent_comment(content_id)

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        return await self.provider.get_conversation_context(content_id, limit)

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int = 5,
    ) -> list[CommunityContentRecord]:
        return await self.provider.get_author_recent_contents(author_id, limit)

    async def get_author_violation_history(
        self,
        author_id: str,
    ) -> list[ViolationRecord]:
        return await self.provider.get_author_violation_history(author_id)

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]:
        return await self.provider.get_content_reports(content_id)

    async def load_context(
        self,
        *,
        content_id: str,
        content_type: ModerationContentType,
        author_id: str | None,
    ) -> ModerationContextEvidence:
        errors: list[str] = []
        snapshot = await self._read(
            "get_content_context",
            self.get_content_context(content_id),
            errors,
            None,
        )
        current = snapshot.current if snapshot is not None else None
        resolved_author_id = author_id or (current.author_id if current else None)
        parent_required = snapshot.parent_comment_required if snapshot is not None else False

        parent: CommunityContentRecord | None = None
        conversation: list[CommunityContentRecord] = []
        if content_type == ModerationContentType.COMMENT:
            parent = await self._read(
                "get_parent_comment",
                self.get_parent_comment(content_id),
                errors,
                None,
            )
            conversation = await self._read(
                "get_conversation_context",
                self.get_conversation_context(content_id, 10),
                errors,
                [],
            )

        recent: list[CommunityContentRecord] = []
        violations: list[ViolationRecord] = []
        if resolved_author_id:
            recent = await self._read(
                "get_author_recent_contents",
                self.get_author_recent_contents(resolved_author_id, 5),
                errors,
                [],
            )
            violations = await self._read(
                "get_author_violation_history",
                self.get_author_violation_history(resolved_author_id),
                errors,
                [],
            )

        reports: list[ReportEvidence] = await self._read(
            "get_content_reports",
            self.get_content_reports(content_id),
            errors,
            [],
        )

        if current is not None and current.content_type != content_type:
            errors.append("content type mismatch")
        if parent_required and parent is None:
            errors.append("required parent comment missing")

        return ModerationContextEvidence(
            current=current,
            post=snapshot.post if snapshot is not None else None,
            parent_comment=parent,
            conversation_context=conversation,
            author_recent_contents=recent,
            author_violation_history=violations,
            reports=reports,
            parent_comment_required=parent_required,
            complete=not errors,
            errors=errors,
        )

    async def _read(
        self,
        operation: str,
        call: Awaitable[T],
        errors: list[str],
        default: T,
    ) -> T:
        try:
            return await call
        except Exception:
            logger.exception("Community provider operation %s failed", operation)
            errors.append(f"{operation} failed")
            return default


class DelegatingCommunityContextLoader:
    def __init__(self, backend: CommunityContextLoader | None = None) -> None:
        self._backend = backend or EmptyCommunityContextLoader()

    def configure(self, backend: CommunityContextLoader) -> None:
        self._backend = backend

    def reset(self) -> None:
        self._backend = EmptyCommunityContextLoader()

    async def load_context(
        self,
        *,
        content_id: str,
        content_type: ModerationContentType,
        author_id: str | None,
    ) -> ModerationContextEvidence | None:
        return await self._backend.load_context(
            content_id=content_id,
            content_type=content_type,
            author_id=author_id,
        )

    async def get_parent_comment(
        self,
        content_id: str,
    ) -> CommunityContentRecord | None:
        backend = self._backend
        if not isinstance(backend, CommunityEvidenceReader):
            return None
        return await backend.get_parent_comment(content_id)

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        backend = self._backend
        if not isinstance(backend, CommunityEvidenceReader):
            return []
        return await backend.get_conversation_context(content_id, limit)

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int = 10,
    ) -> list[CommunityContentRecord]:
        backend = self._backend
        if not isinstance(backend, CommunityEvidenceReader):
            return []
        return await backend.get_author_recent_contents(author_id, limit)

    async def get_author_violation_history(
        self,
        author_id: str,
    ) -> list[ViolationRecord]:
        backend = self._backend
        if not isinstance(backend, CommunityEvidenceReader):
            return []
        return await backend.get_author_violation_history(author_id)

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]:
        backend = self._backend
        if not isinstance(backend, CommunityEvidenceReader):
            return []
        return await backend.get_content_reports(content_id)


default_community_context_loader = DelegatingCommunityContextLoader()
