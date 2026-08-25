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
from datetime import UTC, datetime, timedelta
from typing import Any

from greenbook_agent_core.time_parser import TemporalBase, parse_natural_schedule_time
from greenbook_contracts.tool_result import OperationReceipt, ResourceRef, ToolResult
from greenbook_java_client.models import (
    DraftResponse,
    PublishNowRequest,
    ScheduleCreateRequest,
    ScheduledPublicationResponse,
    ScheduleStatus,
    ScheduleUpdateRequest,
)

from ..context import ToolContext

logger = logging.getLogger(__name__)

_MODIFIABLE_STATUSES = {ScheduleStatus.SCHEDULED.value}


def _schedule_ref(schedule: ScheduledPublicationResponse) -> ResourceRef:
    return ResourceRef(
        ref=f"schedule:{schedule.schedule_id}",
        kind="SCHEDULE",
        resource_id=schedule.schedule_id,
        version=schedule.version,
    )


def _schedule_receipt(
    *,
    operation_id: str,
    semantic_action: str,
    schedule: ScheduledPublicationResponse,
    result_known: bool,
    status: str,
    verification_evidence: dict[str, Any] | None = None,
    request_sent: bool = True,
    downstream_accepted: bool = True,
    side_effect_started: bool = True,
) -> OperationReceipt:
    return OperationReceipt(
        operation_id=operation_id,
        semantic_action=semantic_action,
        resource_ref=_schedule_ref(schedule),
        idempotency_key=operation_id,
        request_sent=request_sent,
        downstream_accepted=downstream_accepted,
        side_effect_started=side_effect_started,
        result_known=result_known,
        observed_state={
            "schedule_id": schedule.schedule_id,
            "draft_id": schedule.draft_id,
            "run_at": schedule.run_at.isoformat() if schedule.run_at else None,
            "timezone": schedule.timezone,
            "status": schedule.status,
            "version": schedule.version,
        },
        verification_evidence=verification_evidence,
        status=status,
    )


def _unknown_schedule_write(
    *,
    ctx: ToolContext,
    operation_id: str,
    semantic_action: str,
    schedule: ScheduledPublicationResponse,
    message: str,
    receipt_id: str | None = None,
    verification_evidence: dict[str, Any] | None = None,
) -> ToolResult[Any]:
    return ToolResult.result_unknown(
        message,
        trace_id=ctx.trace_id,
        receipt_id=receipt_id,
        resource_refs=[_schedule_ref(schedule)],
        operation_receipt=_schedule_receipt(
            operation_id=operation_id,
            semantic_action=semantic_action,
            schedule=schedule,
            result_known=False,
            status="RESULT_UNKNOWN",
            verification_evidence=verification_evidence,
        ),
        state={
            "semantic_action": semantic_action,
            "operation_id": operation_id,
            "downstream_accepted": True,
            "side_effect_started": True,
            "result_known": False,
        },
    )


def _same_instant(left: str, right: datetime | None) -> bool:
    if right is None:
        return False
    try:
        expected = datetime.fromisoformat(left.replace("Z", "+00:00"))
    except ValueError:
        return False
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=UTC)
    actual = right if right.tzinfo is not None else right.replace(tzinfo=UTC)
    # MySQL stores run_at as TIMESTAMP(3): the model's microsecond-precision
    # estimate is rounded to milliseconds on write.  Compare within the
    # storage tolerance instead of demanding bit-exact instants.
    return abs(actual - expected) <= timedelta(milliseconds=1)


def _resolve_update_run_at(
    raw_run_at: str,
    *,
    temporal_base: str,
    current_schedule_run_at: datetime | None,
    timezone: str,
) -> tuple[str | None, str | None]:
    """Resolve an update time from an explicit, declared temporal base.

    For ``EXISTING_SCHEDULE_TIME`` the base is the just-read Java schedule,
    never a session cache or the model's estimate.  This function runs before
    the PUT, so an unresolved expression cannot accidentally reach Java as a
    malformed write.
    """

    try:
        base = TemporalBase(
            str(temporal_base or TemporalBase.CURRENT_TIME)
            .strip()
            .upper()
            .replace("-", "_")
            .replace(" ", "_")
        )
    except ValueError:
        return None, f"Unknown temporal_base: {temporal_base}"

    try:
        explicit = datetime.fromisoformat(str(raw_run_at).replace("Z", "+00:00"))
    except ValueError:
        explicit = None
    if explicit is not None and explicit.tzinfo is not None:
        return explicit.astimezone(UTC).isoformat().replace("+00:00", "Z"), None

    if base == TemporalBase.EXISTING_SCHEDULE_TIME:
        if current_schedule_run_at is None:
            return None, "EXISTING_SCHEDULE_TIME requires the authoritative schedule run_at"
        reference_time = (
            current_schedule_run_at
            if current_schedule_run_at.tzinfo is not None
            else current_schedule_run_at.replace(tzinfo=UTC)
        )
    else:
        reference_time = None

    parsed = parse_natural_schedule_time(
        str(raw_run_at),
        timezone,
        now=reference_time,
    )
    if not parsed:
        return None, f"Could not resolve publication time: {raw_run_at}"
    return parsed, None


async def schedule(
    ctx: ToolContext,
    run_at: str,
    draft_id: str | None = None,
    timezone: str = "Asia/Shanghai",
    requires_approval: bool = False,
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

    # This is a request-level safety constraint, not a second policy catalog:
    # ordinary future scheduling remains available, while an explicit user
    # request for confirmation never crosses the Java write boundary first.
    if requires_approval and not ctx.approval_granted:
        return ToolResult.failure(
            "APPROVAL_REQUIRED",
            "The user requested approval before creating this publication schedule.",
            user_message="创建发布排期前需要你的确认。",
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
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CREATE_SCHEDULE",
            schedule=schedule_data,
            message=f"Schedule {schedule_id} was created but could not be verified",
            receipt_id=result.receipt_id,
            verification_evidence={"get_schedule_ok": False},
        )

    verified = verify.data
    if not isinstance(verified, ScheduledPublicationResponse):
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CREATE_SCHEDULE",
            schedule=schedule_data,
            message=f"Schedule {schedule_id} returned an invalid verification response",
            receipt_id=result.receipt_id,
        )
    if (
        verified.schedule_id != schedule_id
        or verified.draft_id != resolved_draft
        or verified.status != ScheduleStatus.SCHEDULED.value
        or not _same_instant(run_at, verified.run_at)
    ):
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CREATE_SCHEDULE",
            schedule=verified,
            message=f"Schedule {schedule_id} postcondition verification mismatch",
            receipt_id=result.receipt_id,
            verification_evidence={
                "expected_draft_id": resolved_draft,
                "actual_draft_id": verified.draft_id,
                "expected_run_at": run_at,
                "actual_run_at": verified.run_at.isoformat() if verified.run_at else None,
                "actual_status": verified.status,
            },
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
            _schedule_ref(verified),
            ResourceRef(ref=f"draft:{resolved_draft}", kind="DRAFT", resource_id=resolved_draft),
        ],
        operation_receipt=_schedule_receipt(
            operation_id=idempotency_key,
            semantic_action="CREATE_SCHEDULE",
            schedule=verified,
            result_known=True,
            status="COMPLETED",
            verification_evidence={
                "schedule_id_matches": True,
                "draft_id_matches": True,
                "run_at_matches": True,
                "status": ScheduleStatus.SCHEDULED.value,
            },
        ),
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
    timezone: str = "Asia/Shanghai",
    temporal_base: str = TemporalBase.CURRENT_TIME.value,
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

    resolved_run_at, temporal_error = _resolve_update_run_at(
        run_at,
        temporal_base=temporal_base,
        current_schedule_run_at=current_data.run_at,
        timezone=timezone or current_data.timezone or "Asia/Shanghai",
    )
    if temporal_error or not resolved_run_at:
        return ToolResult.validation_error(
            temporal_error or "Invalid schedule time",
            user_message="无法确定新的发布时间，本次尚未修改定时任务。",
        )

    # PUT update
    idempotency_key = ctx.idempotency_key(
        "update_schedule",
        scope=f"{resolved_id}|{resolved_run_at}",
    )
    update_req = ScheduleUpdateRequest(runAt=resolved_run_at, version=current_version)

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

    # Verify the same schedule resource after the PUT.  A successful PUT with
    # an unavailable or inconsistent verification is not reported as success.
    verify = await ctx.java.get_schedule(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verify.ok:
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_SCHEDULE",
            schedule=current_data,
            message=f"Schedule {resolved_id} was updated but could not be verified",
            receipt_id=result.receipt_id,
            verification_evidence={"get_schedule_ok": False},
        )
    verified = verify.data
    if not isinstance(verified, ScheduledPublicationResponse):
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_SCHEDULE",
            schedule=current_data,
            message=f"Schedule {resolved_id} returned an invalid verification response",
            receipt_id=result.receipt_id,
            verification_evidence={"response_type": type(verified).__name__},
        )
    if verified.schedule_id != resolved_id:
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_SCHEDULE",
            schedule=verified,
            message=f"Schedule verification ID mismatch for {resolved_id}",
            receipt_id=result.receipt_id,
            verification_evidence={
                "expected_schedule_id": resolved_id,
                "actual_schedule_id": verified.schedule_id,
            },
        )
    if verified.status != ScheduleStatus.SCHEDULED.value:
        return ToolResult.business_rejected(
            f"Schedule {resolved_id} changed to unexpected status {verified.status}",
            user_message="定时任务更新时间后状态异常，请检查任务状态。",
        )
    if not _same_instant(resolved_run_at, verified.run_at):
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_SCHEDULE",
            schedule=verified,
            message=f"Schedule {resolved_id} runAt verification mismatch",
            receipt_id=result.receipt_id,
            verification_evidence={
                "expected_run_at": resolved_run_at,
                "actual_run_at": verified.run_at.isoformat() if verified.run_at else None,
            },
        )
    if verified.version is None or verified.version <= current_version:
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_SCHEDULE",
            schedule=verified,
            message=f"Schedule {resolved_id} version did not advance after update",
            receipt_id=result.receipt_id,
            verification_evidence={
                "previous_version": current_version,
                "actual_version": verified.version,
            },
        )

    ctx.session.active_schedule_id = resolved_id
    ctx.session.record_entity(
        ref=f"schedule:{resolved_id}",
        kind="SCHEDULE",
        entity_id=resolved_id,
        label=f"Schedule for draft {verified.draft_id or ctx.session.active_draft_id}",
        status=verified.status,
        run_id=ctx.agent_run_id,
    )
    return ToolResult.success(
        {
            "schedule_id": verified.schedule_id,
            "draft_id": verified.draft_id,
            "run_at": verified.run_at.isoformat() if verified.run_at else resolved_run_at,
            "timezone": verified.timezone or timezone or "Asia/Shanghai",
            "status": verified.status,
            "version": verified.version,
        },
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[_schedule_ref(verified)],
        operation_receipt=_schedule_receipt(
            operation_id=idempotency_key,
            semantic_action="UPDATE_SCHEDULE",
            schedule=verified,
            result_known=True,
            status="COMPLETED",
            verification_evidence={
                "schedule_id_matches": True,
                "run_at_matches": True,
                "version_advanced_from": current_version,
                "version": verified.version,
            },
        ),
    )


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
        return current

    current_data = current.data
    if isinstance(current_data, ScheduledPublicationResponse):
        current_status = current_data.status or ""
    else:
        return ToolResult.internal_error("Unexpected schedule response", trace_id=ctx.trace_id)

    # Already CANCELLED → idempotent success
    if current_status == ScheduleStatus.CANCELLED.value:
        ctx.session.active_schedule_id = None
        return ToolResult.success(
            {"schedule_id": resolved_id, "status": "CANCELLED", "already_cancelled": True},
            trace_id=ctx.trace_id,
            resource_refs=[_schedule_ref(current_data)],
            operation_receipt=_schedule_receipt(
                operation_id=ctx.idempotency_key("cancel_schedule", scope=resolved_id),
                semantic_action="CANCEL_SCHEDULE",
                schedule=current_data,
                result_known=True,
                status="COMPLETED",
                verification_evidence={"already_cancelled": True},
                request_sent=False,
                downstream_accepted=False,
                side_effect_started=False,
            ),
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
    if not verify.ok or not isinstance(verify.data, ScheduledPublicationResponse):
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CANCEL_SCHEDULE",
            schedule=current_data,
            message=f"Schedule {resolved_id} cancellation was accepted but could not be verified",
            receipt_id=result.receipt_id,
            verification_evidence={"get_schedule_ok": verify.ok},
        )
    verified = verify.data
    if verified.schedule_id != resolved_id or verified.status != ScheduleStatus.CANCELLED.value:
        return _unknown_schedule_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CANCEL_SCHEDULE",
            schedule=verified,
            message=f"Schedule {resolved_id} cancellation postcondition verification mismatch",
            receipt_id=result.receipt_id,
            verification_evidence={
                "expected_status": ScheduleStatus.CANCELLED.value,
                "actual_status": verified.status,
            },
        )

    ctx.session.active_schedule_id = None

    return ToolResult.success(
        {"schedule_id": resolved_id, "status": ScheduleStatus.CANCELLED.value},
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[_schedule_ref(verified)],
        operation_receipt=_schedule_receipt(
            operation_id=idempotency_key,
            semantic_action="CANCEL_SCHEDULE",
            schedule=verified,
            result_known=True,
            status="COMPLETED",
            verification_evidence={"status": ScheduleStatus.CANCELLED.value},
        ),
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

    if ctx.approval_granted:
        return await publish_now_execute(ctx, resolved_draft)

    # Require explicit approval flag (controlled by the Agent Runtime)
    return ToolResult.failure(
        "APPROVAL_REQUIRED",
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
    if not result.ok:
        return result

    # Publishing is a write with visible business consequences.  A successful
    # POST response is not enough to tell the user that the draft is actually
    # published: read the canonical resource back and require its terminal
    # state before emitting a successful ToolResult.
    verify = await ctx.java.get_draft(
        draft_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verify.ok or not isinstance(verify.data, DraftResponse):
        receipt = OperationReceipt(
            operation_id=idempotency_key,
            semantic_action="PUBLISH_NOW",
            resource_ref=ResourceRef(ref=f"draft:{draft_id}", kind="DRAFT", resource_id=draft_id),
            idempotency_key=idempotency_key,
            request_sent=True,
            downstream_accepted=True,
            side_effect_started=True,
            result_known=False,
            verification_evidence={"get_draft_ok": verify.ok},
            status="RESULT_UNKNOWN",
        )
        return ToolResult.result_unknown(
            f"Draft {draft_id} publish request was accepted but could not be verified",
            trace_id=ctx.trace_id,
            receipt_id=result.receipt_id,
            resource_refs=[receipt.resource_ref],
            operation_receipt=receipt,
        )

    verified = verify.data
    if verified.draft_id != draft_id or verified.status != "published":
        receipt = OperationReceipt(
            operation_id=idempotency_key,
            semantic_action="PUBLISH_NOW",
            resource_ref=ResourceRef(
                ref=f"draft:{draft_id}", kind="DRAFT", resource_id=draft_id,
            ),
            idempotency_key=idempotency_key,
            request_sent=True,
            downstream_accepted=True,
            side_effect_started=True,
            result_known=False,
            observed_state={
                "draft_id": verified.draft_id,
                "status": verified.status,
            },
            verification_evidence={
                "expected_draft_id": draft_id,
                "actual_draft_id": verified.draft_id,
                "expected_status": "published",
                "actual_status": verified.status,
            },
            status="RESULT_UNKNOWN",
        )
        return ToolResult.result_unknown(
            f"Draft {draft_id} publish postcondition verification mismatch",
            trace_id=ctx.trace_id,
            receipt_id=result.receipt_id,
            resource_refs=[receipt.resource_ref],
            operation_receipt=receipt,
        )

    data = (
        result.data.model_dump(mode="json")
        if result.data is not None and hasattr(result.data, "model_dump")
        else dict(result.data or {})
    )
    data["status"] = verified.status
    if result.receipt_id:
        data.setdefault("external_operation_id", result.receipt_id)
        data.setdefault("receipt_id", result.receipt_id)
    post_id = str(data.get("post_id") or "").strip()
    resource_refs = [
        ResourceRef(ref=f"draft:{draft_id}", kind="DRAFT", resource_id=draft_id),
    ]
    if post_id:
        resource_refs.append(
            ResourceRef(ref=f"post:{post_id}", kind="POST", resource_id=post_id)
        )
    return ToolResult.success(
        data,
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=resource_refs,
        operation_receipt=OperationReceipt(
            operation_id=idempotency_key,
            semantic_action="PUBLISH_NOW",
            resource_ref=resource_refs[0],
            idempotency_key=idempotency_key,
            request_sent=True,
            downstream_accepted=True,
            side_effect_started=True,
            result_known=True,
            observed_state={
                "draft_id": verified.draft_id,
                "status": verified.status,
            },
            verification_evidence={"status": "published", "draft_id_matches": True},
            status="COMPLETED",
        ),
    )
