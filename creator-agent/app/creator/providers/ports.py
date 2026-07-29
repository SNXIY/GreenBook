from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.creator.providers.models import (
    CommunityAccessScope,
    CommunityCommentPage,
    CommunityCommentSort,
    CommunityCreatorProfile,
    CommunityEngagementReport,
    CommunityPost,
    CommunityPostMetrics,
    CommunityPostPage,
    CommunitySearchRequest,
    CommunitySearchResult,
)


class CreatorCommunityProvider(Protocol):
    backend_name: str

    async def get_creator_profile(
        self,
        scope: CommunityAccessScope,
    ) -> CommunityCreatorProfile | None: ...

    async def get_user_history(
        self,
        scope: CommunityAccessScope,
        *,
        cursor: str | None,
        limit: int,
        statuses: tuple[str, ...],
    ) -> CommunityPostPage: ...

    async def search_posts(
        self,
        scope: CommunityAccessScope,
        request: CommunitySearchRequest,
    ) -> CommunitySearchResult: ...

    async def get_post_detail(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
    ) -> CommunityPost: ...

    async def get_comments(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
        cursor: str | None,
        limit: int,
        parent_id: str | None,
        sort: CommunityCommentSort,
    ) -> CommunityCommentPage: ...

    async def get_post_metrics(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
    ) -> CommunityPostMetrics: ...

    async def get_engagement(
        self,
        scope: CommunityAccessScope,
        *,
        post_ids: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
    ) -> CommunityEngagementReport: ...

    async def aclose(self) -> None: ...
