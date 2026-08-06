"""Interaction tools — list comments and reply via Java Agent Facade."""

from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import ResourceRef, ToolResult
from greenbook_java_client.models import AgentCommentReplyRequest

from ..context import ToolContext

logger = logging.getLogger(__name__)


async def list_comments(
    ctx: ToolContext,
    post_id: str,
    cursor: str | None = None,
    size: int = 20,
) -> ToolResult[Any]:
    """List comments on a post."""
    result = await ctx.java.list_comments(
        post_id,
        cursor=cursor,
        size=size,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
        )
    return result


async def send_reply(
    ctx: ToolContext,
    post_id: str,
    parent_comment_id: str,
    content: str,
) -> ToolResult[Any]:
    """Reply to a comment. Requires approval."""
    # Approval check
    pending = ctx.session.pending_approval
    if not (pending and pending.operation == "interaction.send_reply"):
        return ToolResult.business_rejected(
            "send_reply requires user approval",
            user_message="发送回复需要用户确认。请确认回复内容。",
        )

    idempotency_key = ctx.idempotency_key(
        "reply",
        scope=f"{post_id}|{parent_comment_id}|{content}",
    )
    reply = AgentCommentReplyRequest(
        postId=post_id,
        parentCommentId=parent_comment_id,
        content=content,
    )

    result = await ctx.java.reply_to_comment(
        reply,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=ctx.trace_id,
            receipt_id=result.receipt_id,
            resource_refs=[
                ResourceRef(ref=f"comment:{result.data.id}", kind="COMMENT", resource_id=result.data.id),
            ],
        )
    return result
