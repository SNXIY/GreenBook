"""Analytics tools — post performance and account summary via Java Agent Facade."""

from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import DataProvenance, ToolResult

from ..context import ToolContext

logger = logging.getLogger(__name__)


def _mark_source(result: ToolResult[Any], source: DataProvenance) -> ToolResult[Any]:
    if not result.ok:
        return result
    return result.model_copy(update={"provenance": [source]})


async def get_post_performance(
    ctx: ToolContext,
    post_id: str,
) -> ToolResult[dict[str, Any]]:
    """Get performance metrics for a post."""
    result = await ctx.java.get_post_analytics(
        post_id,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
            provenance=[DataProvenance.PERSONAL_DATA],
        )
    return _mark_source(result, DataProvenance.PERSONAL_DATA)


async def get_account_summary(
    ctx: ToolContext,
) -> ToolResult[dict[str, Any]]:
    """Get analytics summary for the current user's account."""
    result = await ctx.java.get_account_summary(
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
            provenance=[DataProvenance.PERSONAL_DATA],
        )
    return _mark_source(result, DataProvenance.PERSONAL_DATA)
