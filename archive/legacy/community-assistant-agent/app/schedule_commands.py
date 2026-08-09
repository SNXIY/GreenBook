"""publication.schedule / cancel_schedule ToolRuntime handlers + shared cancel service."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

import httpx

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
from app.write_tools import resolve_schedule_run_at

logger = logging.getLogger(__name__)


class ScheduleCreateReconcile(StrEnum):
    CONFIRMED_CREATED = "CONFIRMED_CREATED"
    CONFIRMED_NOT_CREATED = "CONFIRMED_NOT_CREATED"
    CONFLICTING_STATE = "CONFLICTING_STATE"
    STILL_UNKNOWN = "STILL_UNKNOWN"


class ScheduleCancelReconcile(StrEnum):
    CONFIRMED_CANCELLED = "CONFIRMED_CANCELLED"
    CONFIRMED_NOT_CANCELLED = "CONFIRMED_NOT_CANCELLED"
    ALREADY_EXECUTED = "ALREADY_EXECUTED"
    CONFLICTING_STATE = "CONFLICTING_STATE"
    STILL_UNKNOWN = "STILL_UNKNOWN"


@dataclass
class ScheduleCommandServices:
    schedules: ScheduleRepository
    ledger: SideEffectLedger
    community: CommunityClient
    publication_min_lead_seconds: int = 15
    publication_max_schedule_days: int = 6
    consume_budget: Any | None = None
    run_prompt_loader: Any | None = None


def _cleanup_fields(
    *,
    pending: bool,
    capability_id: str | None,
    reason: str,
    error: str | None = None,
    attempts: int = 1,
) -> dict[str, Any]:
    return {
        "capability_cleanup_pending": pending,
        "capability_id": capability_id,
        "cleanup_reason": reason,
        "cleanup_attempts": attempts,
        "last_cleanup_error": error,
        "next_cleanup_at": None,
    }


async def _revoke_best_effort(
    community: CommunityClient,
    *,
    access_token: str,
    capability_id: str | None,
    reason: str,
) -> tuple[bool, str | None]:
    if not capability_id:
        return True, None
    try:
        await community.revoke_capability(
            access_token=access_token,
            capability_id=capability_id,
        )
        return True, None
    except Exception as exc:
        logger.warning(
            "Capability %s could not be revoked (%s)",
            capability_id,
            reason,
            exc_info=True,
        )
        return False, str(exc)[:500]


def _is_definite_capability_rejection(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code != 408 and code != 429
    return False


def _matches_created_expectation(
    actual: ScheduleSnapshot, expected: dict[str, Any]
) -> bool:
    return (
        actual.action_id == str(expected.get("action_id") or actual.action_id)
        and (actual.idempotency_key or "")
        == str(expected.get("idempotency_key") or "")
        and actual.draft_id == str(expected["draft_id"])
        and actual.expected_content_sha256.lower()
        == str(expected["expected_content_sha256"]).lower()
        and as_utc(actual.run_at).isoformat()
        == as_utc(
            datetime.fromisoformat(
                str(expected["run_at"]).replace("Z", "+00:00")
            )
        ).isoformat()
        and actual.status == str(expected.get("status") or "SCHEDULED")
        and (actual.capability_id or None)
        == (expected.get("capability_id") or None)
    )


async def reconcile_create_schedule(
    *,
    schedules: ScheduleRepository,
    operation_key: str,
    ledger_state: dict[str, Any],
) -> tuple[ScheduleCreateReconcile, ScheduleSnapshot | None]:
    expected = dict(ledger_state.get("expected") or {})
    actual = await schedules.get_by_idempotency_key(idempotency_key=operation_key)
    if actual is None:
        if ledger_state.get("issued_capability_id"):
            return ScheduleCreateReconcile.STILL_UNKNOWN, None
        return ScheduleCreateReconcile.CONFIRMED_NOT_CREATED, None
    if not expected:
        # Row exists for this operation_key — treat as created when fields present.
        return ScheduleCreateReconcile.CONFIRMED_CREATED, actual
    if _matches_created_expectation(actual, expected):
        return ScheduleCreateReconcile.CONFIRMED_CREATED, actual
    # Same key, mismatched payload — conflict.
    return ScheduleCreateReconcile.CONFLICTING_STATE, actual


async def reconcile_cancel_schedule(
    *,
    schedules: ScheduleRepository,
    user_id: str,
    action_id: str,
) -> tuple[ScheduleCancelReconcile, ScheduleSnapshot | None]:
    actual = await schedules.read_snapshot(action_id=action_id, user_id=user_id)
    if actual is None:
        return ScheduleCancelReconcile.STILL_UNKNOWN, None
    if actual.status == "CANCELLED":
        return ScheduleCancelReconcile.CONFIRMED_CANCELLED, actual
    if actual.status in {"SCHEDULED", "RETRYING"}:
        return ScheduleCancelReconcile.CONFIRMED_NOT_CANCELLED, actual
    if actual.status == "COMPLETED":
        return ScheduleCancelReconcile.ALREADY_EXECUTED, actual
    if actual.status == "RUNNING":
        return ScheduleCancelReconcile.CONFLICTING_STATE, actual
    return ScheduleCancelReconcile.CONFLICTING_STATE, actual


# ---------------------------------------------------------------------------
# Shared cancel command (ToolRuntime + HTTP)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CancelScheduleResult:
    output: dict[str, Any]
    old_capability_id: str | None
    noop: bool
    already_cancelled: bool
    outcome: str


async def cancel_schedule_command(
    schedules: ScheduleRepository,
    *,
    action_id: str,
    user_id: str,
) -> CancelScheduleResult:
    """Authoritative local cancel. Caller performs best-effort revoke."""

    outcome, snap, old_cap = await schedules.cancel_cas(
        action_id=action_id, user_id=user_id
    )
    if outcome == "not_found":
        raise ValueError("定时发布任务不存在或不属于当前用户")
    if outcome == "already_executing":
        raise LookupError(
            f"定时发布任务当前状态为 {snap.status}，正在执行中，不能取消"
        )
    if outcome == "already_executed":
        raise LookupError(
            f"定时发布任务当前状态为 {snap.status}，已经发布，不能取消"
        )
    if outcome == "terminal_failed":
        raise LookupError(
            f"定时发布任务当前状态为 {snap.status}，不能取消"
        )
    output = {
        "action_id": snap.action_id,
        "draft_id": snap.draft_id,
        "run_at": snap.run_at.isoformat(),
        "status": "CANCELLED",
    }
    return CancelScheduleResult(
        output=output,
        old_capability_id=old_cap,
        noop=outcome == "already_cancelled",
        already_cancelled=outcome == "already_cancelled",
        outcome=outcome,
    )


async def revoke_after_cancel(
    community: CommunityClient,
    *,
    access_token: str,
    capability_id: str | None,
) -> dict[str, Any]:
    revoked, err = await _revoke_best_effort(
        community,
        access_token=access_token,
        capability_id=capability_id,
        reason="cancel-schedule",
    )
    pending = bool(capability_id) and not revoked
    return _cleanup_fields(
        pending=pending,
        capability_id=capability_id,
        reason="cancel-schedule",
        error=err,
    )


# ---------------------------------------------------------------------------
# publication.schedule
# ---------------------------------------------------------------------------


async def handle_create_schedule(
    *,
    services: ScheduleCommandServices,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    definition: ToolDefinition,
    credentials: ToolCredentials,
    deadline_at: datetime | None,
    attempt_trace: ToolAttemptTrace | None,
    ordinal: int = 0,
    **_kwargs: Any,
) -> dict[str, Any]:
    del definition, deadline_at
    tool_name = "publication.schedule"
    draft_id = str(arguments["draft_id"])
    expected_sha = str(arguments["expected_content_sha256"]).lower()
    record = await services.ledger.prepare(
        run_id=context.run_id,
        ordinal=ordinal,
        tool_name=tool_name,
        arguments=arguments,
        resource_id=f"post:{draft_id}",
    )
    operation_key = record.operation_key
    if attempt_trace is not None:
        attempt_trace.metadata["side_effect_id"] = record.id
        attempt_trace.metadata["operation_key_hash"] = stable_hash(operation_key)

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

    # Resume: always look up existing ScheduledAction before any issue.
    existing = await services.schedules.get_by_idempotency_key(
        idempotency_key=operation_key
    )
    if existing is not None:
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_attempted"] = True
            attempt_trace.metadata["reconciliation_result"] = (
                ScheduleCreateReconcile.CONFIRMED_CREATED.value
            )
        output = {
            "action_id": existing.action_id,
            "draft_id": existing.draft_id,
            "run_at": existing.run_at.isoformat(),
            "status": existing.status,
        }
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="COMPLETED",
            output=output,
            ledger_state={
                **ledger_state,
                "expected": {
                    "action_id": existing.action_id,
                    "idempotency_key": operation_key,
                    "draft_id": existing.draft_id,
                    "expected_content_sha256": existing.expected_content_sha256,
                    "run_at": existing.run_at.isoformat(),
                    "status": existing.status,
                    "capability_id": existing.capability_id,
                },
            },
            remote_operation_id=existing.action_id,
        )
        if attempt_trace is not None:
            attempt_trace.metadata["replayed"] = not record.first_execution
        return {**output, "_runtime_reconciled": True}

    if record.status in {"UNKNOWN", "IN_FLIGHT"} and not record.first_execution:
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_attempted"] = True
        outcome, actual = await reconcile_create_schedule(
            schedules=services.schedules,
            operation_key=operation_key,
            ledger_state=ledger_state,
        )
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_result"] = outcome.value
        if outcome == ScheduleCreateReconcile.CONFIRMED_CREATED and actual is not None:
            output = {
                "action_id": actual.action_id,
                "draft_id": actual.draft_id,
                "run_at": actual.run_at.isoformat(),
                "status": actual.status,
            }
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="COMPLETED",
                output=output,
                ledger_state=ledger_state,
                remote_operation_id=actual.action_id,
            )
            return {**output, "_runtime_reconciled": True}
        if outcome == ScheduleCreateReconcile.CONFIRMED_NOT_CREATED:
            issued = ledger_state.get("issued_capability_id")
            revoked, err = await _revoke_best_effort(
                services.community,
                access_token=credentials.access_token,
                capability_id=str(issued) if issued else None,
                reason="confirmed-not-created cleanup",
            )
            cleanup = _cleanup_fields(
                pending=bool(issued) and not revoked,
                capability_id=str(issued) if issued else None,
                reason="confirmed-not-created cleanup",
                error=err,
            )
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED" if revoked or not issued else "UNKNOWN",
                ledger_state={**ledger_state, **cleanup},
                error="schedule 未创建；禁止盲目重签 Capability",
            )
            if revoked or not issued:
                raise RuntimeError("schedule 未创建；禁止盲目重签 Capability")
            raise UnknownSideEffectError(
                "Capability 可能已签发且清理失败，禁止盲目重签",
                operation_key=operation_key,
            )
        if outcome == ScheduleCreateReconcile.CONFLICTING_STATE:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED",
                ledger_state={
                    **ledger_state,
                    "actual": actual.as_dict() if actual else None,
                },
                error="定时任务与本次操作期望冲突",
            )
            raise LookupError("定时任务与本次操作期望冲突")
        raise UnknownSideEffectError(
            "publication.schedule 结果仍未知，禁止再次签发 Capability",
            operation_key=operation_key,
        )

    # Fresh path — freeze absolute run_at before any remote call.
    now = datetime.now(timezone.utc)
    try:
        run_at = resolve_schedule_run_at(arguments, now=now)
        if run_at <= now + timedelta(seconds=services.publication_min_lead_seconds):
            raise ValueError("定时发布时间必须至少晚于当前时间 15 秒")
        ttl_seconds = int((run_at - now).total_seconds()) + 3_600
        max_ttl = int(
            timedelta(days=services.publication_max_schedule_days).total_seconds()
        )
        if ttl_seconds > max_ttl:
            raise ValueError("定时发布目前最多可提前约 6 天安排")
    except ValueError as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="FAILED",
            error=str(exc),
        )
        raise

    expected = {
        "idempotency_key": operation_key,
        "draft_id": draft_id,
        "expected_content_sha256": expected_sha,
        "run_at": run_at.isoformat(),
        "status": "SCHEDULED",
    }
    ledger_state = {
        "expected": expected,
        "frozen_run_at": run_at.isoformat(),
        "draft_id": draft_id,
        "expected_content_sha256": expected_sha,
    }
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=operation_key,
        ledger_state=ledger_state,
    )

    # Re-check before issue (race with concurrent resume).
    existing = await services.schedules.get_by_idempotency_key(
        idempotency_key=operation_key
    )
    if existing is not None:
        output = {
            "action_id": existing.action_id,
            "draft_id": existing.draft_id,
            "run_at": existing.run_at.isoformat(),
            "status": existing.status,
        }
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="COMPLETED",
            output=output,
            ledger_state={
                **ledger_state,
                "expected": {
                    **expected,
                    "action_id": existing.action_id,
                    "capability_id": existing.capability_id,
                },
            },
            remote_operation_id=existing.action_id,
        )
        return output

    try:
        grant: CapabilityGrant = await services.community.issue_capability(
            access_token=credentials.access_token,
            run_id=context.run_id,
            actions=["publication.publish_now"],
            resources=[f"post:{draft_id}"],
            ttl_seconds=ttl_seconds,
            max_uses=5,
            trace_id=credentials.trace_id,
        )
    except Exception as exc:
        if _is_definite_capability_rejection(exc):
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED",
                ledger_state=ledger_state,
                error=f"capability issue rejected: {exc}",
            )
            raise
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="UNKNOWN",
            ledger_state=ledger_state,
            error=f"capability issue unknown: {exc}",
        )
        raise UnknownSideEffectError(
            f"Capability 签发结果未知，禁止盲目重签：{exc}",
            operation_key=operation_key,
        ) from exc

    if attempt_trace is not None:
        attempt_trace.internal_call_count += 1
        attempt_trace.metadata["issued_capability_id"] = grant.capability_id

    ledger_state.update(
        {
            "issued_capability_id": grant.capability_id,
            "issued_capability_resource": f"post:{draft_id}",
            "issued_capability_expires_at": grant.expires_at,
            "expected": {**expected, "capability_id": grant.capability_id},
        }
    )
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=operation_key,
        ledger_state=ledger_state,
    )

    instruction = None
    if services.run_prompt_loader is not None:
        try:
            instruction = await services.run_prompt_loader(context.run_id)
        except Exception:
            instruction = None

    try:
        snap, created = await services.schedules.create_idempotent(
            run_id=context.run_id,
            user_id=context.user_id,
            draft_id=draft_id,
            expected_content_sha256=expected_sha,
            run_at=run_at,
            idempotency_key=operation_key,
            capability_id=grant.capability_id,
            capability_token_plain=grant.token,
            instruction=instruction,
        )
    except Exception as exc:
        revoked, err = await _revoke_best_effort(
            services.community,
            access_token=credentials.access_token,
            capability_id=grant.capability_id,
            reason="insert-failure cleanup",
        )
        # Confirm whether a row appeared despite the exception.
        after = await services.schedules.get_by_idempotency_key(
            idempotency_key=operation_key
        )
        cleanup = _cleanup_fields(
            pending=not revoked,
            capability_id=grant.capability_id,
            reason="insert-failure cleanup",
            error=err,
        )
        if after is not None:
            output = {
                "action_id": after.action_id,
                "draft_id": after.draft_id,
                "run_at": after.run_at.isoformat(),
                "status": after.status,
            }
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="COMPLETED",
                output=output,
                ledger_state={**ledger_state, **cleanup, "capability_cleanup_pending": False},
                remote_operation_id=after.action_id,
            )
            return output
        if revoked:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED",
                ledger_state={
                    **ledger_state,
                    **cleanup,
                    "reconciliation_result": ScheduleCreateReconcile.CONFIRMED_NOT_CREATED.value,
                },
                error=str(exc),
            )
            raise RuntimeError(f"创建定时任务失败且已回滚 Capability：{exc}") from exc
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="UNKNOWN",
            ledger_state={
                **ledger_state,
                **cleanup,
                "reconciliation_result": ScheduleCreateReconcile.STILL_UNKNOWN.value,
            },
            error=str(exc),
        )
        raise UnknownSideEffectError(
            f"Capability 已签发但 schedule INSERT 失败且吊销未确认：{exc}",
            operation_key=operation_key,
        ) from exc

    if not created and snap.capability_id != grant.capability_id:
        # Lost the race — revoke the unused grant.
        revoked, err = await _revoke_best_effort(
            services.community,
            access_token=credentials.access_token,
            capability_id=grant.capability_id,
            reason="duplicate-create cleanup",
        )
        cleanup = _cleanup_fields(
            pending=not revoked,
            capability_id=grant.capability_id,
            reason="duplicate-create cleanup",
            error=err,
        )
    else:
        cleanup = _cleanup_fields(
            pending=False,
            capability_id=None,
            reason="none",
        )

    output = {
        "action_id": snap.action_id,
        "draft_id": snap.draft_id,
        "run_at": snap.run_at.isoformat(),
        "status": snap.status,
    }
    await services.ledger.finish(
        run_id=context.run_id,
        operation_key=operation_key,
        status="COMPLETED",
        output=output,
        ledger_state={
            **ledger_state,
            **cleanup,
            "expected": {
                **expected,
                "action_id": snap.action_id,
                "capability_id": snap.capability_id,
            },
        },
        remote_operation_id=snap.action_id,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["action_id"] = snap.action_id
        attempt_trace.metadata["capability_cleanup_pending"] = cleanup[
            "capability_cleanup_pending"
        ]
        # Never persist raw tokens in traces.
        assert "token" not in attempt_trace.metadata
    return output


# ---------------------------------------------------------------------------
# publication.cancel_schedule
# ---------------------------------------------------------------------------


async def handle_cancel_schedule(
    *,
    services: ScheduleCommandServices,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    definition: ToolDefinition,
    credentials: ToolCredentials,
    deadline_at: datetime | None,
    attempt_trace: ToolAttemptTrace | None,
    ordinal: int = 0,
    **_kwargs: Any,
) -> dict[str, Any]:
    del definition, deadline_at
    tool_name = "publication.cancel_schedule"
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
        attempt_trace.metadata["operation_key_hash"] = stable_hash(
            record.operation_key
        )

    if record.status == "COMPLETED":
        output = completed_output(record)
        if output is None:
            raise RuntimeError("COMPLETED SideEffect missing cancel output")
        if attempt_trace is not None:
            attempt_trace.metadata["replayed"] = True
            ledger = (record.result or {}).get("ledger") if record.result else None
            attempt_trace.metadata["noop"] = bool(
                isinstance(ledger, dict) and ledger.get("noop")
            )
            attempt_trace.metadata["already_cancelled"] = bool(
                isinstance(ledger, dict) and ledger.get("already_cancelled")
            )
        return {**output, "_runtime_replayed": True}

    if record.first_execution and services.consume_budget is not None:
        await services.consume_budget(context.run_id, "tool")

    ledger_state = ledger_from_record(record)

    if record.status in {"UNKNOWN", "IN_FLIGHT"} and not record.first_execution:
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_attempted"] = True
        outcome, actual = await reconcile_cancel_schedule(
            schedules=services.schedules,
            user_id=context.user_id,
            action_id=action_id,
        )
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_result"] = outcome.value
        if outcome == ScheduleCancelReconcile.CONFIRMED_CANCELLED and actual is not None:
            output = {
                "action_id": actual.action_id,
                "draft_id": actual.draft_id,
                "run_at": actual.run_at.isoformat(),
                "status": "CANCELLED",
            }
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=record.operation_key,
                status="COMPLETED",
                output=output,
                ledger_state={**ledger_state, "noop": True, "already_cancelled": True},
            )
            if attempt_trace is not None:
                attempt_trace.metadata["noop"] = True
                attempt_trace.metadata["already_cancelled"] = True
            return {**output, "_runtime_reconciled": True}
        if outcome == ScheduleCancelReconcile.ALREADY_EXECUTED:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=record.operation_key,
                status="FAILED",
                ledger_state=ledger_state,
                error="定时发布任务已经发布，不能取消",
            )
            raise LookupError("定时发布任务已经发布，不能取消")
        if outcome == ScheduleCancelReconcile.CONFIRMED_NOT_CANCELLED:
            # Fall through to fresh cancel.
            pass
        elif outcome != ScheduleCancelReconcile.CONFIRMED_NOT_CANCELLED:
            raise UnknownSideEffectError(
                "publication.cancel_schedule 结果仍未知",
                operation_key=record.operation_key,
            )

    try:
        result = await cancel_schedule_command(
            services.schedules,
            action_id=action_id,
            user_id=context.user_id,
        )
    except LookupError as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="FAILED",
            error=str(exc),
        )
        raise
    except ValueError as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="FAILED",
            error=str(exc),
        )
        raise

    cleanup: dict[str, Any] = {}
    if result.noop:
        cleanup = _cleanup_fields(
            pending=False, capability_id=None, reason="already-cancelled-noop"
        )
    else:
        cleanup = await revoke_after_cancel(
            services.community,
            access_token=credentials.access_token,
            capability_id=result.old_capability_id,
        )
        if attempt_trace is not None and result.old_capability_id:
            attempt_trace.internal_call_count += 1

    await services.ledger.finish(
        run_id=context.run_id,
        operation_key=record.operation_key,
        status="COMPLETED",
        output=result.output,
        ledger_state={
            **ledger_state,
            **cleanup,
            "noop": result.noop,
            "already_cancelled": result.already_cancelled,
        },
        remote_operation_id=action_id,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["noop"] = result.noop
        attempt_trace.metadata["already_cancelled"] = result.already_cancelled
        attempt_trace.metadata["replayed"] = False
        attempt_trace.metadata["capability_cleanup_pending"] = cleanup.get(
            "capability_cleanup_pending", False
        )
    return result.output


def register_schedule_command_handlers(
    runtime: Any,
    *,
    services: ScheduleCommandServices,
) -> None:
    async def create_handler(**kwargs: Any) -> dict[str, Any]:
        return await handle_create_schedule(services=services, **kwargs)

    async def cancel_handler(**kwargs: Any) -> dict[str, Any]:
        return await handle_cancel_schedule(services=services, **kwargs)

    runtime.register_or_replace_handler("publication.schedule", create_handler)
    runtime.register_or_replace_handler("publication.cancel_schedule", cancel_handler)
