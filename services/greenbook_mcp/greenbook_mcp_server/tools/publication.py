"""Publication tools — schedule, update, cancel, and publish-now.

publication.schedule:
  1. Requires draftId
  2. Convert relative time to absolute time with timezone
  3. Call Java to create Schedule
  4. GET verify — confirm status SCHEDULED, draftId matches

publication.update_schedule:
  1. Resolve active_schedule_id
  2. GET current schedule
  3. If status not modifiable, return business message
  4. PUT with version
  5. GET verify runAt and version

publication.cancel_schedule:
  1. Resolve active_schedule_id
  2. GET current status
  3. SCHEDULED → cancel
  4. Already CANCELLED → idempotent success
  5. PROCESSING → cannot cancel
  6. PUBLISHED → cannot cancel

publication.publish_now:
  Requires approval — tool must not call Java without valid Approval.
"""

from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import ResourceRef, ToolResult
from greenbook_java_client.models import (
    PublishNowRequest,
    ScheduleCreateRequest,
    ScheduledPublicationResponse,
    ScheduleStatus,
    ScheduleUpdateRequest,
)

from ..context import ToolContext

logger = logging.getLogger(__name__)

_MODIFIABLE_STATUSES = {ScheduleStatus.SCHEDULED.value}


async def schedule(
    ctx: ToolContext,
    draft_id: str | None,
    run_at: str,
    timezone: str = "Asia/Shanghai",
) -> ToolResult[Any]:
    """Schedule a draft for publication."""
    resolved_draft = draft_id or ctx.session.active_draft_id
    if not resolved_draft:
        resolved_draft, candidates = ctx.session.resolve_active_draft_id()
        if not resolved_draft:
            return ToolResult.validation_error(
                "No draft specified for scheduling.",
                user_message="请指定要定时发布的草稿。",
            )

    idempotency_key = ctx.idempotency_key(
        "schedule",
        scope=f"{resolved_draft}|{run_at}|{timezone}",
    )

    create_req = ScheduleCreateRequest(
        draftId=resolved_draft,
        runAt=run_at,
        timezone=timezone,
    )
    result = await ctx.java.create_schedule(
        create_req,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not result.ok:
        return result

    schedule_data = result.data
    if not isinstance(schedule_data, ScheduledPublicationResponse):
        return ToolResult.internal_error("Unexpected schedule response", trace_id=ctx.trace_id)

    schedule_id = schedule_data.schedule_id

    # Verify: GET schedule
    verify = await ctx.java.get_schedule(
        schedule_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verify.ok:
        return ToolResult.internal_error(
            f"Schedule {schedule_id} created but GET verification failed",
            trace_id=ctx.trace_id,
        )

    verified = verify.data
    if isinstance(verified, ScheduledPublicationResponse):
        if verified.draft_id != resolved_draft:
            return ToolResult.internal_error(
                f"Schedule draft mismatch: expected={resolved_draft} actual={verified.draft_id}",
                trace_id=ctx.trace_id,
            )
        if verified.status != ScheduleStatus.SCHEDULED.value:
            return ToolResult.internal_error(
                f"Schedule status unexpected: {verified.status}",
                trace_id=ctx.trace_id,
            )

    # Update session
    ctx.session.active_schedule_id = schedule_id
    ctx.session.active_draft_id = resolved_draft
    ctx.session.record_entity(
        ref=f"schedule:{schedule_id}", kind="SCHEDULE", entity_id=schedule_id,
        label=f"Schedule for draft {resolved_draft}", status="SCHEDULED",
        run_id=ctx.agent_run_id,
    )

    return ToolResult.success(
        {
            "schedule_id": schedule_id,
            "draft_id": verified.draft_id if isinstance(verified, ScheduledPublicationResponse) else resolved_draft,
            "run_at": verified.run_at.isoformat() if isinstance(verified, ScheduledPublicationResponse) and verified.run_at else run_at,
            "timezone": verified.timezone if isinstance(verified, ScheduledPublicationResponse) else timezone,
            "status": ScheduleStatus.SCHEDULED.value,
            "version": verified.version if isinstance(verified, ScheduledPublicationResponse) else None,
        },
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[
            ResourceRef(ref=f"schedule:{schedule_id}", kind="SCHEDULE", resource_id=schedule_id),
            ResourceRef(ref=f"draft:{resolved_draft}", kind="DRAFT", resource_id=resolved_draft),
        ],
    )


async def get_status(
    ctx: ToolContext,
    schedule_id: str | None = None,
) -> ToolResult[Any]:
    """Get the current status of a scheduled publication."""
    resolved_id = schedule_id or ctx.session.active_schedule_id
    if not resolved_id:
        resolved_id, candidates = ctx.session.resolve_active_schedule_id()
        if not resolved_id:
            return ToolResult.not_found("No active schedule in current session")

    result = await ctx.java.get_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
        )
    return result


async def update_schedule(
    ctx: ToolContext,
    schedule_id: str | None,
    run_at: str,
) -> ToolResult[Any]:
    """Update a scheduled publication's run_at time."""
    # Resolve schedule_id
    resolved_id = schedule_id or ctx.session.active_schedule_id
    if not resolved_id:
        resolved_id, candidates = ctx.session.resolve_active_schedule_id()
        if not resolved_id and candidates:
            return ToolResult.validation_error(
                "Multiple schedules found. Please specify which one.",
                user_message="当前有多个定时任务，请问您要修改哪一个？",
            )
        if not resolved_id:
            return ToolResult.not_found("No schedule to update.")

    # GET current schedule
    current = await ctx.java.get_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not current.ok:
        return current

    current_data = current.data
    if isinstance(current_data, ScheduledPublicationResponse):
        current_status = current_data.status or ""
        current_version = current_data.version if current_data.version is not None else 0
    else:
        return ToolResult.internal_error("Unexpected schedule response", trace_id=ctx.trace_id)

    if current_status not in _MODIFIABLE_STATUSES:
        status_messages = {
            "CANCELLED": "该定时任务已经取消，无法修改。",
            "PUBLISHED": "该定时任务已经发布完成，无法修改。",
            "PROCESSING": "该定时任务正在执行中，无法修改。",
            "FAILED": "该定时任务已经失败，无法修改。",
        }
        return ToolResult.business_rejected(
            f"Schedule {resolved_id} is in status {current_status}",
            user_message=status_messages.get(current_status, f"当前状态 {current_status} 不支持修改。"),
        )

    # PUT update
    idempotency_key = ctx.idempotency_key(
        "update_schedule",
        scope=f"{resolved_id}|{run_at}",
    )
    update_req = ScheduleUpdateRequest(runAt=run_at, version=current_version)

    result = await ctx.java.update_schedule(
        resolved_id,
        update_req,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not result.ok:
        return result

    # Verify
    verify = await ctx.java.get_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if verify.ok:
        ctx.session.active_schedule_id = resolved_id

    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json") if hasattr(result.data, "model_dump") else result.data,
            trace_id=ctx.trace_id,
        )
    return result


async def cancel_schedule(
    ctx: ToolContext,
    schedule_id: str | None = None,
) -> ToolResult[Any]:
    """Cancel a scheduled publication.

    SCHEDULED → cancel
    Already CANCELLED → idempotent success
    PROCESSING → cannot claim cancellation successful
    PUBLISHED → cannot cancel
    """
    resolved_id = schedule_id or ctx.session.active_schedule_id
    if not resolved_id:
        resolved_id, candidates = ctx.session.resolve_active_schedule_id()
        if not resolved_id and candidates:
            return ToolResult.validation_error(
                "Multiple schedules. Please specify which one to cancel.",
                user_message="当前有多个定时任务，请问您要取消哪一个？",
            )
        if not resolved_id:
            return ToolResult.not_found("No schedule to cancel.")

    # GET current status first
    current = await ctx.java.get_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not current.ok:
        if current.code == "NOT_FOUND":
            return ToolResult.success(
                {"schedule_id": resolved_id, "status": "CANCELLED"},
                trace_id=ctx.trace_id,
            )
        return current

    current_data = current.data
    if isinstance(current_data, ScheduledPublicationResponse):
        current_status = current_data.status or ""
    else:
        current_status = ""

    # Already CANCELLED → idempotent success
    if current_status == ScheduleStatus.CANCELLED.value:
        ctx.session.active_schedule_id = None
        return ToolResult.success(
            {"schedule_id": resolved_id, "status": "CANCELLED", "already_cancelled": True},
            trace_id=ctx.trace_id,
        )

    # PUBLISHED → cannot cancel
    if current_status == ScheduleStatus.PUBLISHED.value:
        return ToolResult.business_rejected(
            "Cannot cancel a published schedule",
            user_message="该定时任务已经发布完成，无法取消。",
        )

    # PROCESSING → do not lie about cancellation
    if current_status == ScheduleStatus.PROCESSING.value:
        return ToolResult.business_rejected(
            "Schedule is currently processing",
            user_message="该定时任务正在执行中，暂时无法取消。请等待执行完成后查看状态。",
        )

    # FAILED → cannot cancel
    if current_status == ScheduleStatus.FAILED.value:
        return ToolResult.business_rejected(
            "Cannot cancel a failed schedule",
            user_message="该定时任务已经失败，无需取消。",
        )

    # SCHEDULED → cancel
    idempotency_key = ctx.idempotency_key("cancel_schedule", scope=resolved_id)
    result = await ctx.java.cancel_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not result.ok:
        return result

    # Verify final state
    verify = await ctx.java.get_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    final_status = ScheduleStatus.CANCELLED.value
    if verify.ok and isinstance(verify.data, ScheduledPublicationResponse):
        final_status = verify.data.status or final_status

    ctx.session.active_schedule_id = None

    return ToolResult.success(
        {"schedule_id": resolved_id, "status": final_status},
        trace_id=ctx.trace_id,
    )


async def publish_now(
    ctx: ToolContext,
    draft_id: str | None = None,
) -> ToolResult[Any]:
    """Immediately publish a draft.

    REQUIRES APPROVAL. Without valid approval, tool must not call Java.
    """
    resolved_draft = draft_id or ctx.session.active_draft_id
    if not resolved_draft:
        resolved_draft, candidates = ctx.session.resolve_active_draft_id()
        if not resolved_draft:
            return ToolResult.validation_error(
                "No draft specified for publishing.",
                user_message="请指定要发布的草稿。",
            )

    # Check for pending approval
    pending = ctx.session.pending_approval
    if (
        pending
        and pending.operation == "publication.publish_now"
        and pending.resource_id
        and pending.resource_id != resolved_draft
    ):
            return ToolResult.validation_error(
                "Approval resource mismatch",
                user_message="审批资源与原请求不匹配，请重新确认。",
            )

    # Require explicit approval flag (controlled by the assistant)
    return ToolResult.business_rejected(
        "publish_now requires explicit user approval",
        user_message=(
            "立即发布需要用户确认。请确认是否发布该草稿。"
        ),
    )


async def publish_now_execute(
    ctx: ToolContext,
    draft_id: str,
) -> ToolResult[Any]:
    """Execute publish after approval. Only called when approval is valid."""
    idempotency_key = ctx.idempotency_key("publish_now", scope=str(draft_id))
    request = PublishNowRequest(draftId=draft_id)

    result = await ctx.java.publish_now(
        request,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json") if hasattr(result.data, "model_dump") else result.data,
            trace_id=ctx.trace_id,
            resource_refs=[
                ResourceRef(ref=f"draft:{draft_id}", kind="DRAFT", resource_id=draft_id),
            ],
        )
    return result
