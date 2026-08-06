"""Community tools — search, read, list posts via Java Agent Facade."""

from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import ToolResult
from greenbook_java_client.models import SortMode

from ..context import ToolContext

logger = logging.getLogger(__name__)


async def search_public_posts(
    ctx: ToolContext,
    query: str,
    sort: str = "latest",
    page: int = 1,
    size: int = 20,
) -> ToolResult[dict[str, Any]]:
    """Search public posts in the GreenBook community."""
    return await ctx.java.search_posts(
        query=query,
        sort=sort,
        page=page,
        size=size,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )


async def get_post(
    ctx: ToolContext,
    post_id: str,
) -> ToolResult[dict[str, Any]]:
    """Get a single post by ID."""
    result = await ctx.java.get_post(
        post_id,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
        )
    return result


async def list_own_posts(
    ctx: ToolContext,
    page: int = 1,
    size: int = 20,
) -> ToolResult[dict[str, Any]]:
    """List current user's own posts."""
    result = await ctx.java.list_own_posts(
        page=page,
        size=size,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        items = [item.model_dump(mode="json") for item in result.data]
        return ToolResult.success(items, trace_id=result.trace_id)
    return result
