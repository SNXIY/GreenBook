from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from app.creator.providers.errors import (
    CreatorCommunityCapabilityError,
    CreatorCommunityNotFoundError,
    CreatorCommunityScopeError,
    CreatorCommunityUnavailableError,
)
from app.creator.providers.models import (
    CommunityAccessScope,
    CommunityComment,
    CommunityCommentPage,
    CommunityCommentSort,
    CommunityCreatorProfile,
    CommunityEngagementReport,
    CommunityPost,
    CommunityPostMetrics,
    CommunityPostPage,
    CommunitySearchCandidate,
    CommunitySearchRequest,
    CommunitySearchResult,
)


class JavaCreatorCommunityProvider:
    """HMAC-authenticated adapter for Zhiguang creator internal APIs."""

    backend_name = "zhiguang-java"

    def __init__(
        self,
        *,
        base_url: str,
        shared_secret: str,
        service_name: str = "mindflow-creator",
        allowed_tenant_id: str = "",
        timeout_seconds: float = 8.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Java community base URL is required")
        if not shared_secret:
            raise ValueError("Java community shared secret is required")
        self._base_url = base_url.rstrip("/")
        self._secret = shared_secret
        self._service_name = service_name
        self._allowed_tenant_id = allowed_tenant_id.strip()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def get_creator_profile(
        self,
        scope: CommunityAccessScope,
    ) -> CommunityCreatorProfile | None:
        data = await self._request(
            scope,
            "GET",
            "/internal/v1/creator/writing-profile",
        )
        if not data:
            return None
        tags = _parse_tags(data.get("tagJson"))
        return CommunityCreatorProfile(
            creator_id=str(data.get("userId") or scope.creator_id),
            display_name=str(data.get("nickname") or ""),
            bio=str(data.get("bio") or ""),
            expertise_tags=tags,
            source_system=self.backend_name,
        )

    async def get_user_history(
        self,
        scope: CommunityAccessScope,
        *,
        cursor: str | None,
        limit: int,
        statuses: tuple[str, ...],
    ) -> CommunityPostPage:
        del scope, cursor, limit, statuses
        raise CreatorCommunityCapabilityError(
            "Zhiguang creator history endpoint is not available yet"
        )

    async def search_posts(
        self,
        scope: CommunityAccessScope,
        request: CommunitySearchRequest,
    ) -> CommunitySearchResult:
        payload = {
            "queries": list(request.queries),
            "filters": {
                "tags": list(request.tags),
                "author_ids": list(request.creator_ids),
                "content_types": list(request.content_types),
                "published_after": _iso(request.published_after),
                "published_before": _iso(request.published_before),
                "visibility": ["public"],
            },
            "bm25TopK": request.limit,
            "knnTopK": request.limit,
        }
        data = await self._request(
            scope,
            "POST",
            "/internal/v1/creator/search",
            payload=payload,
        )
        raw_candidates = [
            *(data.get("bm25") or []),
            *(data.get("knn") or []),
        ]
        candidates: list[CommunitySearchCandidate] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_candidates:
            post_id = str(raw.get("postId") or "")
            channel = str(raw.get("channel") or "JAVA_SEARCH")
            if not post_id or (post_id, channel) in seen:
                continue
            seen.add((post_id, channel))
            post = _post_from_search(scope.tenant_id, raw)
            candidates.append(
                CommunitySearchCandidate(
                    post=post,
                    score=max(0.0, float(raw.get("score") or 0.0)),
                    rank=max(1, int(raw.get("rank") or len(candidates) + 1)),
                    channel=channel,
                    metadata=dict(raw.get("metadata") or {}),
                )
            )
        candidates.sort(key=lambda item: (item.score, -item.rank), reverse=True)
        return CommunitySearchResult(
            candidates=tuple(candidates[: request.limit]),
            degraded_services=tuple(
                str(value) for value in (data.get("degradedServices") or ())
            ),
        )

    async def get_post_detail(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
    ) -> CommunityPost:
        post, _ = await self._post_context(
            scope,
            post_id=post_id,
            include_comments=False,
            comment_limit=0,
        )
        return post

    async def get_comments(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
        cursor: str | None,
        limit: int,
        parent_id: str | None,
        sort: CommunityCommentSort,
    ) -> CommunityCommentPage:
        if parent_id is not None or sort == CommunityCommentSort.HOT:
            raise CreatorCommunityCapabilityError(
                "Zhiguang creator context currently exposes recent top-level comments only"
            )
        _, comments = await self._post_context(
            scope,
            post_id=post_id,
            include_comments=True,
            comment_limit=min(20, limit),
        )
        offset = _decode_cursor(cursor)
        page = comments[offset : offset + limit]
        next_offset = offset + len(page)
        return CommunityCommentPage(
            items=tuple(page),
            next_cursor=(
                _encode_cursor(next_offset) if next_offset < len(comments) else None
            ),
            has_more=next_offset < len(comments),
        )

    async def get_post_metrics(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
    ) -> CommunityPostMetrics:
        del scope, post_id
        raise CreatorCommunityCapabilityError(
            "Zhiguang creator metrics endpoint is not available yet"
        )

    async def get_engagement(
        self,
        scope: CommunityAccessScope,
        *,
        post_ids: tuple[str, ...],
        start: datetime | None,
        end: datetime | None,
    ) -> CommunityEngagementReport:
        del scope, post_ids, start, end
        raise CreatorCommunityCapabilityError(
            "Zhiguang creator engagement endpoint is not available yet"
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post_context(
        self,
        scope: CommunityAccessScope,
        *,
        post_id: str,
        include_comments: bool,
        comment_limit: int,
    ) -> tuple[CommunityPost, tuple[CommunityComment, ...]]:
        data = await self._request(
            scope,
            "POST",
            "/internal/v1/creator/posts/context",
            payload={
                "postIds": [post_id],
                "includeComments": include_comments,
                "commentLimit": comment_limit,
            },
        )
        posts = data.get("posts") or []
        if not posts:
            raise CreatorCommunityNotFoundError(
                f"Post {post_id} was not found",
                details={"post_id": post_id},
            )
        raw = posts[0]
        post = CommunityPost(
            tenant_id=scope.tenant_id,
            post_id=str(raw.get("postId") or post_id),
            creator_id=str(raw.get("authorId") or ""),
            creator_name=str(raw.get("authorName") or ""),
            title=str(raw.get("title") or "Untitled"),
            body=str(raw.get("body") or ""),
            description=str(raw.get("description") or ""),
            tags=tuple(str(value) for value in (raw.get("tags") or ())),
            visibility=str(raw.get("visibility") or "public"),
            content_type=str(raw.get("contentType") or "image_text"),
            status=str(raw.get("status") or ""),
            published_at=_datetime(raw.get("publishedAt")),
            source_system=self.backend_name,
        )
        comments = tuple(
            CommunityComment(
                tenant_id=scope.tenant_id,
                comment_id=str(item.get("commentId") or ""),
                post_id=post.post_id,
                author_id=str(item.get("authorId") or ""),
                author_name=str(item.get("authorName") or ""),
                content=str(item.get("content") or ""),
                created_at=_datetime(item.get("createdAt")) or post.updated_at,
            )
            for item in (raw.get("comments") or ())
        )
        return post, comments

    async def _request(
        self,
        scope: CommunityAccessScope,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_tenant(scope)
        body = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if payload is not None
            else b""
        )
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        roles = ",".join(sorted(scope.roles)) or "CREATOR"
        signature = _sign(
            self._secret,
            method=method,
            path=path,
            timestamp=timestamp,
            nonce=nonce,
            user_id=scope.creator_id,
            roles=roles,
            trace_id=scope.trace_id,
            body=body,
        )
        headers = {
            "X-Zhiguang-Service": self._service_name,
            "X-Zhiguang-User-Id": scope.creator_id,
            "X-Zhiguang-Roles": roles,
            "X-Trace-Id": scope.trace_id,
            "X-Zhiguang-Timestamp": timestamp,
            "X-Zhiguang-Nonce": nonce,
            "X-Zhiguang-Signature": signature,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}{path}",
                content=body or None,
                headers=headers,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise CreatorCommunityUnavailableError(
                "Zhiguang community service is unavailable"
            ) from exc
        if response.status_code == 404:
            raise CreatorCommunityNotFoundError("Community resource was not found")
        if response.status_code in {401, 403}:
            raise CreatorCommunityScopeError("Community request was not authorized")
        if response.status_code >= 500:
            raise CreatorCommunityUnavailableError(
                "Zhiguang community service returned a server error"
            )
        if response.status_code >= 400:
            raise CreatorCommunityUnavailableError(
                f"Zhiguang community request failed with HTTP {response.status_code}"
            )
        try:
            envelope = response.json()
        except ValueError as exc:
            raise CreatorCommunityUnavailableError(
                "Zhiguang community response was not valid JSON"
            ) from exc
        if envelope.get("code") != "OK":
            raise CreatorCommunityUnavailableError(
                str(envelope.get("message") or "Zhiguang community request failed")
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise CreatorCommunityUnavailableError(
                "Zhiguang community response data was invalid"
            )
        return data

    def _require_tenant(self, scope: CommunityAccessScope) -> None:
        if self._allowed_tenant_id and scope.tenant_id != self._allowed_tenant_id:
            raise CreatorCommunityScopeError(
                "Tenant is not mapped to this Zhiguang provider"
            )


def _sign(
    secret: str,
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    user_id: str,
    roles: str,
    trace_id: str,
    body: bytes,
) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (
            method.upper(),
            path,
            timestamp,
            nonce,
            user_id,
            roles,
            trace_id,
            body_hash,
        )
    )
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _parse_tags(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if not isinstance(value, str) or not value.strip():
        return ()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(parsed, list):
        return tuple(str(item) for item in parsed)
    return ()


def _post_from_search(tenant_id: str, value: dict[str, Any]) -> CommunityPost:
    excerpt = str(value.get("excerpt") or "")
    return CommunityPost(
        tenant_id=tenant_id,
        post_id=str(value.get("postId") or ""),
        creator_id=str(value.get("authorId") or ""),
        creator_name=str(value.get("authorName") or ""),
        title=str(value.get("title") or "Untitled"),
        description=excerpt,
        body=excerpt,
        tags=tuple(str(item) for item in (value.get("tags") or ())),
        content_type=str(value.get("contentType") or "image_text"),
        visibility=str(value.get("visibility") or "public"),
        status=str(value.get("status") or ""),
        source_url=str(value.get("url") or "") or None,
        published_at=_datetime(value.get("publishedAt")),
        source_system="zhiguang-search",
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return None


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(cursor + padding).decode())
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("Invalid pagination cursor") from exc
    if value < 0:
        raise ValueError("Invalid pagination cursor")
    return value
