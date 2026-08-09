"""Durable SideEffect ledger used by ToolRuntime write paths."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.database import (
    Database,
    Run,
    RunStep,
    SideEffect,
    ToolExecutionReceipt,
    append_event,
    utc_now,
)
from app.revision_claim import (
    RevisionClaimConflict,
    acquire_revision_claim_lock,
    find_conflicting_revision_claim,
    initial_revision_claim_payload,
    register_active_revision_claim,
    release_active_revision_claim,
    revision_claim_lock_key,
    revision_resource_id,
)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class SideEffectRecord:
    id: str
    operation_key: str
    status: str
    request_hash: str
    attempts: int
    result: dict[str, Any] | None
    error: str | None
    first_execution: bool


class SideEffectLedger:
    """Postgres-backed SideEffect + ToolExecutionReceipt boundary."""

    def __init__(self, database: Database, *, worker_id: str) -> None:
        self.database = database
        self.worker_id = worker_id

    async def prepare(
        self,
        *,
        run_id: str,
        ordinal: int,
        tool_name: str,
        arguments: dict[str, Any],
        resource_id: str | None,
        revision_claim: dict[str, str] | None = None,
    ) -> SideEffectRecord:
        """Prepare SideEffect; optional revision_claim enforces draft+sha exclusivity.

        revision_claim keys: user_id, draft_id, base_content_sha256
        """

        request_hash = stable_hash({"tool": tool_name, "arguments": arguments})
        operation_key = f"assistant-effect-{run_id}-{ordinal}"
        claim_resource_id = resource_id
        claim_payload: dict[str, Any] | None = None
        if revision_claim is not None:
            user_id = str(revision_claim["user_id"])
            draft_id = str(revision_claim["draft_id"])
            base_sha = str(revision_claim["base_content_sha256"]).lower()
            claim_resource_id = revision_resource_id(
                user_id=user_id,
                draft_id=draft_id,
                base_content_sha256=base_sha,
            )
            claim_payload = initial_revision_claim_payload(
                user_id=user_id,
                draft_id=draft_id,
                base_content_sha256=base_sha,
            )
        async with self.database.sessions() as session, session.begin():
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能准备副作用")
            if revision_claim is not None:
                await acquire_revision_claim_lock(
                    session,
                    lock_key=revision_claim_lock_key(
                        user_id=str(revision_claim["user_id"]),
                        draft_id=str(revision_claim["draft_id"]),
                        base_content_sha256=str(
                            revision_claim["base_content_sha256"]
                        ).lower(),
                    ),
                )
            effect = await session.scalar(
                select(SideEffect)
                .where(
                    SideEffect.run_id == run_id,
                    SideEffect.step_ordinal == ordinal,
                )
                .with_for_update()
            )
            step = await session.scalar(
                select(RunStep).where(
                    RunStep.run_id == run_id,
                    RunStep.ordinal == ordinal,
                )
            )
            if step is None:
                raise RuntimeError("Cannot prepare a tool execution without a RunStep")
            receipt = await session.scalar(
                select(ToolExecutionReceipt)
                .where(
                    ToolExecutionReceipt.run_id == run_id,
                    ToolExecutionReceipt.step_id == step.id,
                )
                .with_for_update()
            )
            if effect is None:
                if revision_claim is not None and claim_resource_id is not None:
                    conflict = await find_conflicting_revision_claim(
                        session,
                        resource_id=claim_resource_id,
                        base_content_sha256=str(
                            revision_claim["base_content_sha256"]
                        ).lower(),
                        exclude_operation_key=operation_key,
                    )
                    if conflict is not None:
                        raise RevisionClaimConflict(
                            "同一草稿版本已有进行中的修订，禁止并发提交",
                            existing_operation_key=conflict.operation_key,
                            existing_status=conflict.status,
                        )
                    await register_active_revision_claim(
                        session,
                        resource_id=claim_resource_id,
                        operation_key=operation_key,
                        run_id=run_id,
                    )
                first_execution = True
                effect = SideEffect(
                    run_id=run_id,
                    step_ordinal=ordinal,
                    tool_name=tool_name,
                    operation_key=operation_key,
                    request_hash=request_hash,
                    resource_id=claim_resource_id,
                    status="PREPARED",
                    result=claim_payload,
                )
                session.add(effect)
                await session.flush()
                if receipt is None:
                    receipt = ToolExecutionReceipt(
                        run_id=run_id,
                        step_id=step.id,
                        tool_name=tool_name,
                        idempotency_key=operation_key,
                        input_hash=request_hash,
                        status="PREPARED",
                        result_ref=f"side-effect:{effect.id}",
                    )
                    session.add(receipt)
                    await session.flush()
                event_type = "SIDE_EFFECT_PREPARED"
            else:
                first_execution = False
                operation_key = effect.operation_key
                if effect.tool_name != tool_name or effect.request_hash != request_hash:
                    raise RuntimeError(
                        "同一步骤的副作用参数已变化，拒绝复用旧幂等边界"
                    )
                if receipt is None:
                    receipt = ToolExecutionReceipt(
                        run_id=run_id,
                        step_id=step.id,
                        tool_name=tool_name,
                        idempotency_key=effect.operation_key,
                        input_hash=request_hash,
                        status=effect.status,
                        result_ref=f"side-effect:{effect.id}",
                    )
                    session.add(receipt)
                    await session.flush()
                if effect.status == "COMPLETED" and effect.result is not None:
                    receipt.status = "COMPLETED"
                    return SideEffectRecord(
                        id=effect.id,
                        operation_key=operation_key,
                        status="COMPLETED",
                        request_hash=request_hash,
                        attempts=effect.attempts,
                        result=dict(effect.result),
                        error=None,
                        first_execution=False,
                    )
                event_type = (
                    "SIDE_EFFECT_DEPENDENCY_RESUMED"
                    if effect.status == "WAITING_DEPENDENCY"
                    else (
                        "SIDE_EFFECT_RECONCILING"
                        if effect.status in {"UNKNOWN", "IN_FLIGHT"}
                        else "SIDE_EFFECT_RESUMED"
                    )
                )
            # Resume WAITING_DEPENDENCY / UNKNOWN / IN_FLIGHT without inventing a
            # new operation. Fresh PREPARED rows advance to IN_FLIGHT.
            if effect.status == "WAITING_DEPENDENCY":
                effect.status = "IN_FLIGHT"
                effect.attempts += 1
                effect.error = None
            elif effect.status not in {"UNKNOWN", "IN_FLIGHT", "COMPLETED"}:
                effect.status = "IN_FLIGHT"
                effect.attempts += 1
                effect.error = None
            effect.last_reconciled_at = utc_now()
            effect.updated_at = utc_now()
            if (
                receipt.tool_name != tool_name
                or receipt.input_hash != request_hash
                or receipt.idempotency_key != operation_key
            ):
                raise RuntimeError(
                    "Tool execution idempotency key was reused with different input"
                )
            if effect.status != "COMPLETED":
                receipt.status = effect.status
            await append_event(
                session,
                run_id,
                event_type,
                {
                    "tool": tool_name,
                    "ordinal": ordinal,
                    "operation_key_hash": stable_hash(operation_key),
                    "attempt": effect.attempts,
                    "status": effect.status,
                    "resource_id": effect.resource_id,
                },
            )
            return SideEffectRecord(
                id=effect.id,
                operation_key=operation_key,
                status=effect.status,
                request_hash=request_hash,
                attempts=effect.attempts,
                result=dict(effect.result) if effect.result else None,
                error=effect.error,
                first_execution=first_execution,
            )

    async def mark_in_flight(
        self,
        *,
        run_id: str,
        operation_key: str,
        ledger_state: dict[str, Any],
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            effect = await session.scalar(
                select(SideEffect)
                .where(SideEffect.operation_key == operation_key)
                .with_for_update()
            )
            if effect is None:
                return
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 不能推进副作用")
            if effect.status == "PREPARED":
                effect.attempts += 1
            effect.status = "IN_FLIGHT"
            effect.result = {"ledger": ledger_state}
            effect.error = None
            effect.last_reconciled_at = utc_now()
            effect.updated_at = utc_now()
            receipt = await session.scalar(
                select(ToolExecutionReceipt)
                .where(ToolExecutionReceipt.idempotency_key == operation_key)
                .with_for_update()
            )
            if receipt is not None:
                receipt.status = "IN_FLIGHT"
            await append_event(
                session,
                run_id,
                "SIDE_EFFECT_IN_FLIGHT",
                {
                    "operation_key_hash": stable_hash(operation_key),
                    "action_id": ledger_state.get("action_id"),
                    "issued_capability_id": ledger_state.get("issued_capability_id"),
                },
            )

    async def finish(
        self,
        *,
        run_id: str,
        operation_key: str,
        status: str,
        output: dict[str, Any] | None = None,
        ledger_state: dict[str, Any] | None = None,
        error: str | None = None,
        remote_operation_id: str | None = None,
    ) -> None:
        async with self.database.sessions() as session, session.begin():
            effect = await session.scalar(
                select(SideEffect)
                .where(SideEffect.operation_key == operation_key)
                .with_for_update()
            )
            if effect is None:
                return
            run = await session.get(Run, run_id)
            if run is None or run.lease_owner != self.worker_id:
                raise RuntimeError("过期 Worker 的副作用结果已拒绝")
            payload: dict[str, Any] | None
            if status == "WAITING_DEPENDENCY" and ledger_state is not None:
                # Flat continuation so resume can read task_id without nesting.
                payload = dict(ledger_state)
            elif status == "COMPLETED" and output is not None:
                payload = {
                    "output": output,
                    "ledger": ledger_state or (effect.result or {}).get("ledger"),
                }
            elif ledger_state is not None:
                payload = {"ledger": ledger_state}
            else:
                payload = dict(effect.result) if effect.result else None
            effect.status = status
            effect.result = payload
            effect.remote_operation_id = remote_operation_id
            effect.error = error[:4_000] if error else None
            effect.last_reconciled_at = utc_now()
            effect.updated_at = utc_now()
            receipt = await session.scalar(
                select(ToolExecutionReceipt)
                .where(ToolExecutionReceipt.idempotency_key == operation_key)
                .with_for_update()
            )
            if receipt is not None:
                receipt.status = status
                receipt.result_ref = f"side-effect:{effect.id}"
            # Release revise claim when the Creator task is definitively
            # terminal.  Keeping the claim on FAILED was meant to prevent a
            # second revise from bypassing the first, but it also blocks a
            # legitimate retry after a permanent Creator failure.  Only
            # keep the claim while Creator is still in-flight (RUNNING /
            # QUEUED / CREATED / WAITING_DEPENDENCY).
            if (
                status in {"FAILED", "CANCELLED"}
                and effect.tool_name == "creator.revise_draft"
                and effect.resource_id
            ):
                await release_active_revision_claim(
                    session, resource_id=effect.resource_id
                )
            event_payload = {
                "tool": effect.tool_name,
                "operation_key_hash": stable_hash(operation_key),
                "remote_operation_id": remote_operation_id,
                "error": effect.error,
                "capability_cleanup_pending": bool(
                    (ledger_state or {}).get("capability_cleanup_pending")
                ),
                "dependency_status": (ledger_state or {}).get("status"),
            }
            await append_event(
                session,
                run_id,
                f"SIDE_EFFECT_{status}",
                event_payload,
            )

    async def load(self, *, operation_key: str) -> SideEffectRecord | None:
        async with self.database.sessions() as session:
            effect = await session.scalar(
                select(SideEffect).where(SideEffect.operation_key == operation_key)
            )
        if effect is None:
            return None
        return SideEffectRecord(
            id=effect.id,
            operation_key=effect.operation_key,
            status=effect.status,
            request_hash=effect.request_hash,
            attempts=effect.attempts,
            result=dict(effect.result) if effect.result else None,
            error=effect.error,
            first_execution=False,
        )


def completed_output(record: SideEffectRecord) -> dict[str, Any] | None:
    if record.result is None:
        return None
    if "output" in record.result and isinstance(record.result["output"], dict):
        return dict(record.result["output"])
    # Legacy Worker-completed shape: flat schedule fields.
    if "action_id" in record.result and "ledger" not in record.result:
        return dict(record.result)
    return None


def ledger_from_record(record: SideEffectRecord) -> dict[str, Any]:
    if not record.result:
        return {}
    ledger = record.result.get("ledger")
    if isinstance(ledger, dict):
        return dict(ledger)
    # Flat WAITING_DEPENDENCY continuation.
    if "task_id" in record.result or "remote_task_id" in record.result:
        return dict(record.result)
    return {}
