"""Interaction tools — list comments and reply via Java Agent Facade."""

from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import OperationReceipt, ResourceRef, ToolResult
from greenbook_java_client.models import AgentCommentReplyRequest, AgentCommentResponse

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
    if not result.ok or not isinstance(result.data, AgentCommentResponse):
        return result

    created = result.data
    resource_ref = ResourceRef(
        ref=f"comment:{created.id}", kind="COMMENT", resource_id=created.id,
    )
    # A 201 only acknowledges the reply request.  Re-read the canonical Java
    # comment before showing completion; otherwise a lost response or an
    # inconsistent adapter response would become a false user-visible reply.
    verify = await ctx.java.get_comment(
        created.id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verify.ok or not isinstance(verify.data, AgentCommentResponse):
        return ToolResult.result_unknown(
            f"Reply {created.id} was accepted but could not be verified",
            trace_id=ctx.trace_id,
            receipt_id=result.receipt_id,
            resource_refs=[resource_ref],
            operation_receipt=OperationReceipt(
                operation_id=idempotency_key,
                semantic_action="REPLY_COMMENT",
                resource_ref=resource_ref,
                idempotency_key=idempotency_key,
                request_sent=True,
                downstream_accepted=True,
                side_effect_started=True,
                result_known=False,
                verification_evidence={"get_comment_ok": verify.ok},
                status="RESULT_UNKNOWN",
            ),
        )

    verified = verify.data
    mismatches: dict[str, Any] = {}
    if verified.id != created.id:
        mismatches["id"] = {"expected": created.id, "actual": verified.id}
    if verified.post_id != post_id:
        mismatches["post_id"] = {"expected": post_id, "actual": verified.post_id}
    if verified.parent_id != parent_comment_id:
        mismatches["parent_comment_id"] = {
            "expected": parent_comment_id,
            "actual": verified.parent_id,
        }
    if verified.content != content:
        mismatches["content"] = {"expected": content, "actual": verified.content}
    if mismatches:
        return ToolResult.result_unknown(
            f"Reply {created.id} postcondition verification mismatch",
            trace_id=ctx.trace_id,
            receipt_id=result.receipt_id,
            resource_refs=[resource_ref],
            operation_receipt=OperationReceipt(
                operation_id=idempotency_key,
                semantic_action="REPLY_COMMENT",
                resource_ref=resource_ref,
                idempotency_key=idempotency_key,
                request_sent=True,
                downstream_accepted=True,
                side_effect_started=True,
                result_known=False,
                observed_state=verified.model_dump(mode="json"),
                verification_evidence=mismatches,
                status="RESULT_UNKNOWN",
            ),
        )

    return ToolResult.success(
        verified.model_dump(mode="json"),
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[resource_ref],
        operation_receipt=OperationReceipt(
            operation_id=idempotency_key,
            semantic_action="REPLY_COMMENT",
            resource_ref=resource_ref,
            idempotency_key=idempotency_key,
            request_sent=True,
            downstream_accepted=True,
            side_effect_started=True,
            result_known=True,
            observed_state=verified.model_dump(mode="json"),
            verification_evidence={
                "comment_id_matches": True,
                "post_id_matches": True,
                "parent_comment_id_matches": True,
                "content_matches": True,
            },
            status="COMPLETED",
        ),
    )
