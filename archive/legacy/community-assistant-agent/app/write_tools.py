"""First-class write handlers for Phase 5 Step 3 (no Worker dependency)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from app.clients import CapabilityGrant, CommunityClient
from app.schedule_repository import ScheduleRepository, ScheduleSnapshot, as_utc
from app.side_effect_ledger import (
    SideEffectLedger,
    completed_output,
    ledger_from_record,
    stable_hash,
)
from app.tool_runtime import (
    ToolAttemptTrace,
    ToolCredentials,
    ToolInvocationContext,
    UnknownSideEffectError,
)
from app.tools import ToolDefinition

logger = logging.getLogger(__name__)


class ReconciliationResult(StrEnum):
    CONFIRMED_APPLIED = "CONFIRMED_APPLIED"
    CONFIRMED_NOT_APPLIED = "CONFIRMED_NOT_APPLIED"
    CONFLICTING_STATE = "CONFLICTING_STATE"
    STILL_UNKNOWN = "STILL_UNKNOWN"


@dataclass
class UpdateScheduleServices:
    schedules: ScheduleRepository
    ledger: SideEffectLedger
    community: CommunityClient
    publication_min_lead_seconds: int = 15
    publication_max_schedule_days: int = 6
    consume_budget: Any | None = None


def _parse_run_at(value: Any) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed.astimezone(timezone.utc)


def resolve_schedule_run_at(
    arguments: dict[str, Any],
    *,
    now: datetime | None = None,
) -> datetime:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    delay_seconds = arguments.get("delay_seconds")
    if delay_seconds is not None:
        return current + timedelta(seconds=int(delay_seconds))
    return _parse_run_at(arguments.get("run_at"))


def _norm_run_at(value: Any) -> str:
    if isinstance(value, datetime):
        return as_utc(value).isoformat()
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return as_utc(parsed).isoformat()


def _snapshots_equal(actual: ScheduleSnapshot, expected: dict[str, Any]) -> bool:
    return (
        _norm_run_at(actual.run_at) == _norm_run_at(expected["run_at"])
        and actual.draft_id == str(expected["draft_id"])
        and actual.expected_content_sha256.lower()
        == str(expected["expected_content_sha256"]).lower()
    )


def _matches_before(actual: ScheduleSnapshot, before: dict[str, Any]) -> bool:
    return (
        _norm_run_at(actual.run_at) == _norm_run_at(before["run_at"])
        and actual.draft_id == str(before["draft_id"])
        and actual.expected_content_sha256.lower()
        == str(before["expected_content_sha256"]).lower()
        and actual.status == str(before["status"])
        and (actual.capability_id or None)
        == (before.get("capability_id") or None)
    )


async def reconcile_update_schedule(
    *,
    schedules: ScheduleRepository,
    user_id: str,
    ledger_state: dict[str, Any],
) -> tuple[ReconciliationResult, ScheduleSnapshot | None]:
    action_id = str(ledger_state.get("action_id") or "")
    before = dict(ledger_state.get("before") or {})
    expected = dict(ledger_state.get("expected") or {})
    if not action_id or not before or not expected:
        return ReconciliationResult.STILL_UNKNOWN, None
    try:
        actual = await schedules.read_snapshot(action_id=action_id, user_id=user_id)
    except Exception:
        logger.exception("schedule reconcile read failed action_id=%s", action_id)
        return ReconciliationResult.STILL_UNKNOWN, None
    if actual is None:
        return ReconciliationResult.STILL_UNKNOWN, None
    if _snapshots_equal(actual, expected):
        return ReconciliationResult.CONFIRMED_APPLIED, actual
    if _matches_before(actual, before):
        return ReconciliationResult.CONFIRMED_NOT_APPLIED, actual
    return ReconciliationResult.CONFLICTING_STATE, actual


async def _revoke_best_effort(
    community: CommunityClient,
    *,
    credentials: ToolCredentials,
    capability_id: str | None,
    reason: str,
) -> bool:
    if not capability_id:
        return True
    try:
        await community.revoke_capability(
            access_token=credentials.access_token,
            capability_id=capability_id,
        )
        return True
    except Exception:
        logger.warning(
            "Capability %s could not be revoked (%s)",
            capability_id,
            reason,
            exc_info=True,
        )
        return False


async def handle_update_schedule(
    *,
    services: UpdateScheduleServices,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    definition: ToolDefinition,
    credentials: ToolCredentials,
    deadline_at: datetime | None,
    attempt_trace: ToolAttemptTrace | None,
    ordinal: int = 0,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Builtin write: CAS update local ScheduledAction + rotate publish capability."""

    del definition, deadline_at
    tool_name = "publication.update_schedule"
    action_id = str(arguments["action_id"])
    record = await services.ledger.prepare(
        run_id=context.run_id,
        ordinal=ordinal,
        tool_name=tool_name,
        arguments=arguments,
        resource_id=f"schedule:{action_id}",
    )
    if attempt_trace is not None:
        attempt_trace.metadata["side_effect_id"] = record.id
        attempt_trace.metadata["operation_key_hash"] = stable_hash(record.operation_key)
        attempt_trace.metadata["write_phase"] = record.status

    # Replay
    if record.status == "COMPLETED":
        output = completed_output(record)
        if output is None:
            raise RuntimeError("COMPLETED SideEffect missing schedule output")
        if attempt_trace is not None:
            attempt_trace.metadata["replayed"] = True
        return {**output, "_runtime_replayed": True}

    if record.first_execution and services.consume_budget is not None:
        await services.consume_budget(context.run_id, "tool")

    ledger_state = ledger_from_record(record)

    # UNKNOWN / IN_FLIGHT → reconcile first; never enter fresh write on resume.
    if record.status in {"UNKNOWN", "IN_FLIGHT"} and not record.first_execution:
        if not ledger_state.get("expected"):
            raise UnknownSideEffectError(
                "publication.update_schedule 缺少 before/expected 快照，等待核对",
                operation_key=record.operation_key,
            )
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_attempted"] = True
        outcome, actual = await reconcile_update_schedule(
            schedules=services.schedules,
            user_id=context.user_id,
            ledger_state=ledger_state,
        )
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_result"] = outcome.value
        if outcome == ReconciliationResult.CONFIRMED_APPLIED and actual is not None:
            output = {
                "action_id": actual.action_id,
                "draft_id": actual.draft_id,
                "run_at": actual.run_at.isoformat(),
                "status": actual.status,
            }
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=record.operation_key,
                status="COMPLETED",
                output=output,
                ledger_state=ledger_state,
            )
            return {**output, "_runtime_reconciled": True}
        if outcome == ReconciliationResult.CONFLICTING_STATE:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=record.operation_key,
                status="FAILED",
                ledger_state={
                    **ledger_state,
                    "actual": actual.as_dict() if actual else None,
                },
                error="定时发布任务状态与本次操作期望冲突",
            )
            raise LookupError("定时发布任务状态与本次操作期望冲突")
        if outcome == ReconciliationResult.CONFIRMED_NOT_APPLIED:
            issued = ledger_state.get("issued_capability_id")
            revoked = await _revoke_best_effort(
                services.community,
                credentials=credentials,
                capability_id=str(issued) if issued else None,
                reason="confirmed-not-applied cleanup",
            )
            ledger_state["capability_cleanup_pending"] = bool(issued) and not revoked
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=record.operation_key,
                status="FAILED",
                ledger_state=ledger_state,
                error="本地 schedule 更新未提交，禁止盲目重写",
            )
            raise RuntimeError("本地 schedule 更新未提交，禁止盲目重写")
        raise UnknownSideEffectError(
            "publication.update_schedule 结果仍未知，等待核对",
            operation_key=record.operation_key,
        )

    # Fresh write path (first_execution only)
    before_snap = await services.schedules.read_snapshot(
        action_id=action_id, user_id=context.user_id
    )
    if before_snap is None:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="FAILED",
            error="定时发布任务不存在或不属于当前用户",
        )
        raise ValueError("定时发布任务不存在或不属于当前用户")
    if before_snap.status not in {"SCHEDULED", "RETRYING"}:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="FAILED",
            error=f"定时发布任务当前状态为 {before_snap.status}，不能修改",
        )
        raise ValueError(
            f"定时发布任务当前状态为 {before_snap.status}，不能修改"
        )

    now = datetime.now(timezone.utc)
    try:
        if (
            arguments.get("run_at") is not None
            or arguments.get("delay_seconds") is not None
        ):
            target_run_at = resolve_schedule_run_at(arguments, now=now)
        else:
            target_run_at = before_snap.run_at
        if target_run_at <= now + timedelta(
            seconds=services.publication_min_lead_seconds
        ):
            raise ValueError("修改后的发布时间必须至少晚于当前时间 15 秒")
        if target_run_at > now + timedelta(
            days=services.publication_max_schedule_days
        ):
            raise ValueError("定时发布目前最多可提前约 6 天安排")
    except ValueError as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="FAILED",
            error=str(exc),
        )
        raise

    target_draft_id = str(arguments.get("draft_id") or before_snap.draft_id)
    target_sha = str(
        arguments.get("expected_content_sha256")
        or before_snap.expected_content_sha256
    ).lower()

    before = before_snap.as_dict()
    expected = {
        "run_at": target_run_at.isoformat(),
        "draft_id": target_draft_id,
        "expected_content_sha256": target_sha,
        "status": "SCHEDULED",
    }
    # Persist before/expected before any Capability HTTP call.
    ledger_state = {
        "action_id": action_id,
        "before": before,
        "expected": expected,
    }
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=record.operation_key,
        ledger_state=ledger_state,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["write_phase"] = "PREPARED"
        attempt_trace.metadata["action_id"] = action_id

    ttl_seconds = int((target_run_at - now).total_seconds()) + 3_600
    try:
        grant: CapabilityGrant = await services.community.issue_capability(
            access_token=credentials.access_token,
            run_id=context.run_id,
            actions=["publication.publish_now"],
            resources=[f"post:{target_draft_id}"],
            ttl_seconds=ttl_seconds,
            max_uses=5,
            trace_id=credentials.trace_id,
        )
    except Exception as exc:
        # Capability issue may have applied — treat as UNKNOWN, do not re-issue.
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="UNKNOWN",
            ledger_state=ledger_state,
            error=f"capability issue unknown: {exc}",
        )
        raise UnknownSideEffectError(
            f"Capability 签发结果未知，禁止盲目重签：{exc}",
            operation_key=record.operation_key,
        ) from exc

    ledger_state.update(
        {
            "issued_capability_id": grant.capability_id,
            "issued_capability_resource": f"post:{target_draft_id}",
            "issued_capability_expires_at": grant.expires_at,
        }
    )
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=record.operation_key,
        ledger_state=ledger_state,
    )
    if attempt_trace is not None:
        attempt_trace.internal_call_count += 1
        attempt_trace.metadata["write_phase"] = "IN_FLIGHT"
        attempt_trace.metadata["issued_capability_id"] = grant.capability_id

    old_capability_id = before_snap.capability_id
    try:
        after = await services.schedules.cas_update(
            action_id=action_id,
            user_id=context.user_id,
            before=before_snap,
            target_run_at=target_run_at,
            target_draft_id=target_draft_id,
            target_sha=target_sha,
            capability_id=grant.capability_id,
            capability_token_plain=grant.token,
        )
    except LookupError as exc:
        await _revoke_best_effort(
            services.community,
            credentials=credentials,
            capability_id=grant.capability_id,
            reason="cas-conflict cleanup",
        )
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="FAILED",
            ledger_state=ledger_state,
            error=str(exc),
        )
        raise
    except Exception as exc:
        await _revoke_best_effort(
            services.community,
            credentials=credentials,
            capability_id=grant.capability_id,
            reason="local-tx-failure cleanup",
        )
        # Local write may or may not have committed depending on failure mode.
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="UNKNOWN",
            ledger_state=ledger_state,
            error=str(exc),
        )
        raise UnknownSideEffectError(
            f"本地 schedule 更新结果未知：{exc}",
            operation_key=record.operation_key,
        ) from exc

    cleanup_pending = False
    if old_capability_id and old_capability_id != grant.capability_id:
        revoked = await _revoke_best_effort(
            services.community,
            credentials=credentials,
            capability_id=old_capability_id,
            reason="replace old schedule capability",
        )
        cleanup_pending = not revoked
    ledger_state["capability_cleanup_pending"] = cleanup_pending
    ledger_state["after"] = after.as_dict()

    output = {
        "action_id": after.action_id,
        "draft_id": after.draft_id,
        "run_at": after.run_at.isoformat(),
        "status": after.status,
    }
    await services.ledger.finish(
        run_id=context.run_id,
        operation_key=record.operation_key,
        status="COMPLETED",
        output=output,
        ledger_state=ledger_state,
        remote_operation_id=after.action_id,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["write_phase"] = "COMPLETED"
        attempt_trace.metadata["capability_cleanup_pending"] = cleanup_pending
    return output


def register_update_schedule_handler(
    runtime: Any,
    *,
    services: UpdateScheduleServices,
) -> None:
    async def handler(**kwargs: Any) -> dict[str, Any]:
        return await handle_update_schedule(services=services, **kwargs)

    runtime.register_or_replace_handler("publication.update_schedule", handler)
