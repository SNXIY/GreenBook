from typing import Protocol

from moderation.schemas import (
    CommunityContentRecord,
    CommunityContentSnapshot,
    ModerationTaskDetail,
    ReportEvidence,
    ViolationRecord,
)


class CommunityDataProvider(Protocol):
    async def apply_moderation_result(self, task: ModerationTaskDetail) -> None: ...

    async def get_content_context(self, content_id: str) -> CommunityContentSnapshot: ...

    async def get_parent_comment(self, content_id: str) -> CommunityContentRecord | None: ...

    async def get_conversation_context(
        self,
        content_id: str,
        limit: int,
    ) -> list[CommunityContentRecord]: ...

    async def get_author_recent_contents(
        self,
        author_id: str,
        limit: int,
    ) -> list[CommunityContentRecord]: ...

    async def get_author_violation_history(
        self,
        author_id: str,
    ) -> list[ViolationRecord]: ...

    async def get_content_reports(self, content_id: str) -> list[ReportEvidence]: ...
