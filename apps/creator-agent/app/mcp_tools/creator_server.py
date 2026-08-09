import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Literal

from pydantic import AnyHttpUrl

from app.core.config import Settings, get_settings
from app.creator.tools.composition import (
    CreatorToolRuntime,
    open_creator_tool_runtime,
)
from app.creator.tools.errors import CreatorToolError
from app.creator.tools.models import (
    CommentsToolData,
    CreatorProfileToolData,
    CreatorToolResult,
    DraftToolData,
    EngagementToolData,
    PostDetailToolData,
    PostMetricsToolData,
    SearchPostsToolData,
    UserHistoryToolData,
)

try:
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.server.transport_security import TransportSecuritySettings
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Install the bounded MCP v1 dependency from pyproject.toml and uv.lock"
    ) from exc


class StaticCreatorTokenVerifier(TokenVerifier):
    """Service-token verifier for private Streamable HTTP deployments."""

    def __init__(
        self,
        *,
        token: str,
        client_id: str,
    ) -> None:
        self._token = token
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id=self._client_id,
            scopes=["creator:tools"],
        )


def create_creator_mcp_server(
    settings: Settings | None = None,
    *,
    runtime: CreatorToolRuntime | None = None,
) -> FastMCP:
    resolved = settings or get_settings()
    transport = _transport(resolved.creator_mcp_transport)
    token_verifier, auth = _auth(resolved, transport=transport)
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(_csv(resolved.creator_mcp_allowed_hosts)),
        allowed_origins=list(_csv(resolved.creator_mcp_allowed_origins)),
    )

    @asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[CreatorToolRuntime]:
        if runtime is not None:
            yield runtime
            return
        async with open_creator_tool_runtime(settings=resolved) as opened:
            yield opened

    mcp = FastMCP(
        "mindflow-creator-intelligence",
        instructions=(
            "Governed Creator Intelligence tools. Identity and tenant scope are "
            "bound by the server. Publication is intentionally unavailable."
        ),
        host=resolved.creator_mcp_host,
        port=resolved.creator_mcp_port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        lifespan=lifespan,
        token_verifier=token_verifier,
        auth=auth,
        transport_security=transport_security,
    )

    read_annotations = ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    draft_annotations = ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def get_creator_profile(
        ctx: Context,
    ) -> CreatorToolResult[CreatorProfileToolData]:
        """Load the authenticated creator's community and writing profile."""
        return await _invoke_typed(
            ctx,
            "get_creator_profile",
            {},
            CreatorProfileToolData,
        )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def get_user_history(
        ctx: Context,
        cursor: str | None = None,
        limit: int = 20,
        statuses: tuple[str, ...] = ("published",),
    ) -> CreatorToolResult[UserHistoryToolData]:
        """List the authenticated creator's historical posts."""
        return await _invoke_typed(
            ctx,
            "get_user_history",
            {
                "cursor": cursor,
                "limit": limit,
                "statuses": statuses,
            },
            UserHistoryToolData,
        )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def search_posts(
        queries: tuple[str, ...],
        ctx: Context,
        tags: tuple[str, ...] = (),
        creator_ids: tuple[str, ...] = (),
        content_types: tuple[str, ...] = (),
        published_after: datetime | None = None,
        published_before: datetime | None = None,
        intent: str = "TOPIC_RESEARCH",
        limit: int = 10,
    ) -> CreatorToolResult[SearchPostsToolData]:
        """Search authorized community posts through the governed retrieval path."""
        return await _invoke_typed(
            ctx,
            "search_posts",
            {
                "queries": queries,
                "tags": tags,
                "creator_ids": creator_ids,
                "content_types": content_types,
                "published_after": published_after,
                "published_before": published_before,
                "intent": intent,
                "limit": limit,
            },
            SearchPostsToolData,
        )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def get_post_detail(
        post_id: str,
        ctx: Context,
    ) -> CreatorToolResult[PostDetailToolData]:
        """Load one authorized community post with its full body."""
        return await _invoke_typed(
            ctx,
            "get_post_detail",
            {"post_id": post_id},
            PostDetailToolData,
        )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def get_comments(
        post_id: str,
        ctx: Context,
        cursor: str | None = None,
        limit: int = 20,
        parent_id: str | None = None,
        sort: str = "RECENT",
    ) -> CreatorToolResult[CommentsToolData]:
        """List authorized comments for a community post."""
        return await _invoke_typed(
            ctx,
            "get_comments",
            {
                "post_id": post_id,
                "cursor": cursor,
                "limit": limit,
                "parent_id": parent_id,
                "sort": sort,
            },
            CommentsToolData,
        )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def get_post_metrics(
        post_id: str,
        ctx: Context,
    ) -> CreatorToolResult[PostMetricsToolData]:
        """Load metrics for a post owned by the authenticated creator."""
        return await _invoke_typed(
            ctx,
            "get_post_metrics",
            {"post_id": post_id},
            PostMetricsToolData,
        )

    @mcp.tool(annotations=read_annotations, structured_output=True)
    async def get_engagement(
        ctx: Context,
        post_ids: tuple[str, ...] = (),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> CreatorToolResult[EngagementToolData]:
        """Load aggregate and time-series creator engagement."""
        return await _invoke_typed(
            ctx,
            "get_engagement",
            {
                "post_ids": post_ids,
                "start": start,
                "end": end,
            },
            EngagementToolData,
        )

    @mcp.tool(annotations=draft_annotations, structured_output=True)
    async def save_draft(
        task_id: str,
        title: str,
        content_markdown: str,
        idempotency_key: str,
        ctx: Context,
        source_artifact_id: str | None = None,
    ) -> CreatorToolResult[DraftToolData]:
        """Create the first version of a task-owned draft."""
        return await _invoke_typed(
            ctx,
            "save_draft",
            {
                "task_id": task_id,
                "title": title,
                "content_markdown": content_markdown,
                "source_artifact_id": source_artifact_id,
                "idempotency_key": idempotency_key,
            },
            DraftToolData,
            task_id=task_id,
        )

    @mcp.tool(annotations=draft_annotations, structured_output=True)
    async def update_draft(
        draft_id: str,
        expected_version: int,
        content_markdown: str,
        idempotency_key: str,
        ctx: Context,
        title: str | None = None,
        source_artifact_id: str | None = None,
    ) -> CreatorToolResult[DraftToolData]:
        """Append a version to an owned draft using optimistic locking."""
        return await _invoke_typed(
            ctx,
            "update_draft",
            {
                "draft_id": draft_id,
                "expected_version": expected_version,
                "title": title,
                "content_markdown": content_markdown,
                "source_artifact_id": source_artifact_id,
                "idempotency_key": idempotency_key,
            },
            DraftToolData,
        )

    return mcp


async def _invoke_typed(
    ctx: Context,
    name: str,
    arguments: dict[str, Any],
    data_type,
    *,
    task_id: str | None = None,
):
    runtime: CreatorToolRuntime = ctx.request_context.lifespan_context
    trace_id = f"mcp-{ctx.request_id}"[:128]
    try:
        result = await runtime.gateway.call(
            name,
            arguments,
            runtime.context(
                trace_id=trace_id,
                task_id=task_id,
            ),
        )
    except CreatorToolError as exc:
        call = f" call_id={exc.call_id}" if exc.call_id else ""
        raise ToolError(f"{exc.code}:{call} {exc}") from exc
    typed_result = CreatorToolResult[data_type]
    return typed_result.model_validate(result.model_dump(mode="json"))


def _auth(
    settings: Settings,
    *,
    transport: str,
):
    if transport == "stdio":
        return None, None
    token = settings.creator_mcp_bearer_token
    if not token:
        raise ValueError("CREATOR_MCP_BEARER_TOKEN is required for Streamable HTTP")
    verifier = StaticCreatorTokenVerifier(
        token=token,
        client_id=settings.creator_mcp_actor_id,
    )
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(settings.creator_mcp_auth_issuer_url),
        resource_server_url=AnyHttpUrl(settings.creator_mcp_resource_server_url),
        required_scopes=["creator:tools"],
    )
    return verifier, auth


def _transport(value: str) -> Literal["stdio", "streamable-http"]:
    normalized = value.strip().lower()
    if normalized == "stdio":
        return "stdio"
    if normalized == "streamable-http":
        return "streamable-http"
    raise ValueError("CREATOR_MCP_TRANSPORT must be 'stdio' or 'streamable-http'")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )


def main() -> None:
    settings = get_settings()
    transport = _transport(settings.creator_mcp_transport)
    server = create_creator_mcp_server(settings)
    server.run(transport=transport)


if __name__ == "__main__":
    main()
