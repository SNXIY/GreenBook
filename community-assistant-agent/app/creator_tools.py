"""Creator create/revise handlers for Phase 5 Step 4 (no Worker dependency)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.clients import CapabilityGrant, CommunityClient, CreatorClient
from app.revision_claim import RevisionClaimConflict
from app.side_effect_ledger import (
    SideEffectLedger,
    completed_output,
    ledger_from_record,
    stable_hash,
)
from app.tool_dependency import (
    DependencyPending,
    DependencyStatus,
    ToolDependencyDescriptor,
)
from app.tool_runtime import (
    ToolAttemptTrace,
    ToolCredentials,
    ToolInvocationContext,
    UnknownSideEffectError,
)
from app.tools import ToolDefinition

logger = logging.getLogger(__name__)

MIGRATED_CREATOR_TOOLS: frozenset[str] = frozenset(
    {
        "creator.create_draft",
        "creator.revise_draft",
    }
)

# Protocol debt (do not claim Creator natively supports revise):
# Creator API should later add kind=REVISE_CONTENT with base_draft_id +
# expected_content_sha256 + revision_instruction.

IssueCapability = Callable[..., Awaitable[CapabilityGrant]]
ContentTargetLoader = Callable[
    [ToolInvocationContext], Awaitable[dict[str, Any] | None]
]


@dataclass
class CreatorToolServices:
    creator: CreatorClient
    community: CommunityClient
    ledger: SideEffectLedger
    issue_capability: IssueCapability
    consume_budget: Any | None = None
    load_content_target: ContentTargetLoader | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dependency_state(
    *,
    task_id: str,
    status: str,
    submitted_at: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "task_id": task_id,
        "remote_task_id": task_id,
        "submitted_at": submitted_at,
        "status": status,
        "provider": "creator",
        "dependency_type": "CREATOR_TASK",
    }
    if extra:
        payload.update(extra)
    return payload


def _descriptor_from_state(
    *,
    tool_name: str,
    context: ToolInvocationContext,
    operation_key: str,
    side_effect_id: str,
    state: dict[str, Any],
) -> ToolDependencyDescriptor:
    status_raw = str(state.get("status") or "PENDING")
    try:
        dep_status = DependencyStatus(status_raw)
    except ValueError:
        if status_raw in {"QUEUED"}:
            dep_status = DependencyStatus.PENDING
        else:
            dep_status = DependencyStatus.RUNNING
    meta = {
        k: state[k]
        for k in (
            "submitted_at",
            "required_action",
            "display_message",
            "interrupt_id",
            "checkpoint_id",
            "pending_decision_id",
            "operation_mode",
            "source_draft_id",
            "expected_content_sha256",
            "idempotency_recovery",
            "poll_count",
        )
        if k in state
    }
    return ToolDependencyDescriptor(
        provider="creator",
        dependency_type="CREATOR_TASK",
        remote_task_id=str(state.get("remote_task_id") or state.get("task_id")),
        tool_name=tool_name,
        run_id=context.run_id,
        step_id=context.step_id,
        side_effect_id=side_effect_id,
        operation_key=operation_key,
        status=dep_status,
        deadline_at=context.deadline_at,
        metadata=meta,
    )


def _raise_waiting(
    *,
    tool_name: str,
    context: ToolInvocationContext,
    operation_key: str,
    side_effect_id: str,
    state: dict[str, Any],
) -> None:
    descriptor = _descriptor_from_state(
        tool_name=tool_name,
        context=context,
        operation_key=operation_key,
        side_effect_id=side_effect_id,
        state=state,
    )
    raise DependencyPending(
        task_id=descriptor.remote_task_id,
        status=descriptor.status.value,
        state=descriptor.safe_public_dict()
        | {
            "task_id": descriptor.remote_task_id,
            "submitted_at": state.get("submitted_at"),
            "status": descriptor.status.value,
            **{k: v for k, v in state.items() if k not in {"task_id", "status"}},
        },
        dependency_type="CREATOR_TASK",
        descriptor=descriptor,
    )


async def _validate_revise_preconditions(
    *,
    services: CreatorToolServices,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    credentials: ToolCredentials,
) -> dict[str, Any]:
    draft_id = str(arguments["draft_id"])
    expected = str(arguments["expected_content_sha256"]).lower()
    if services.load_content_target is not None:
        target = await services.load_content_target(context)
        if target is None:
            raise LookupError("修订目标草稿未绑定到当前 Goal/Task")
        if str(target.get("draft_id") or "") != draft_id:
            raise LookupError("修订草稿与当前 Goal 绑定不一致")
        target_sha = str(target.get("content_sha256") or "").lower()
        if target_sha and target_sha != expected:
            raise LookupError("目标草稿版本已变化，拒绝基于旧版本修订")

    grant = await services.issue_capability(
        action="community.get_own_draft",
        resources=[f"post:{draft_id}"],
        max_uses=1,
        ttl_seconds=60,
        context=context,
        credentials=credentials,
    )
    draft = await services.community.get_own_draft(
        draft_id,
        capability_token=grant.token,
        trace_id=credentials.trace_id,
    )
    actual = str(
        draft.get("contentSha256") or draft.get("content_sha256") or ""
    ).lower()
    status = str(draft.get("status") or "").upper()
    if status and status not in {"READY", "DRAFT", "AI_DRAFT"}:
        # OwnedDraftOutput requires READY; treat anything else as conflict.
        if status != "READY":
            raise LookupError(f"草稿当前状态为 {status}，不能修订")
    if actual != expected:
        raise LookupError("草稿内容版本已变化，拒绝基于旧版本修订")
    return draft


async def handle_creator_tool(
    *,
    services: CreatorToolServices,
    tool_name: str,
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
    if tool_name not in MIGRATED_CREATOR_TOOLS:
        raise ValueError(f"unsupported creator tool: {tool_name}")

    # TODO(protocol-debt): Creator native REVISE_CONTENT is not available yet.
    # This handler uses CREATE_CONTENT + Assistant SUPERSEDES semantics only.
    resource_id = None
    revision_claim: dict[str, str] | None = None
    if tool_name == "creator.revise_draft":
        revision_claim = {
            "user_id": context.user_id,
            "draft_id": str(arguments["draft_id"]),
            "base_content_sha256": str(arguments["expected_content_sha256"]).lower(),
        }
    try:
        record = await services.ledger.prepare(
            run_id=context.run_id,
            ordinal=ordinal,
            tool_name=tool_name,
            arguments=arguments,
            resource_id=resource_id,
            revision_claim=revision_claim,
        )
    except RevisionClaimConflict as exc:
        if attempt_trace is not None:
            attempt_trace.metadata["revision_claim_conflict"] = True
            attempt_trace.metadata["existing_operation_key_hash"] = (
                stable_hash(exc.existing_operation_key)
                if exc.existing_operation_key
                else None
            )
            attempt_trace.metadata["existing_status"] = exc.existing_status
        raise LookupError(str(exc)) from exc
    if attempt_trace is not None:
        attempt_trace.metadata["side_effect_id"] = record.id
        attempt_trace.metadata["operation_key_hash"] = stable_hash(record.operation_key)

    if record.status == "COMPLETED":
        output = completed_output(record)
        if output is None:
            raise RuntimeError(f"COMPLETED SideEffect missing {tool_name} output")
        if attempt_trace is not None:
            attempt_trace.metadata["replayed"] = True
            if output.get("draft_id"):
                attempt_trace.metadata["artifact_draft_id"] = output["draft_id"]
        return {**output, "_runtime_replayed": True}

    if record.first_execution and services.consume_budget is not None:
        await services.consume_budget(context.run_id, "tool")

    ledger_state = ledger_from_record(record)
    # Normalize: WAITING/UNKNOWN may store dependency under ledger or flat result.
    if not ledger_state and record.result:
        if "task_id" in record.result or "remote_task_id" in record.result:
            ledger_state = dict(record.result)

    # Resume / UNKNOWN recovery — never blind create.
    remote_task_id = str(
        ledger_state.get("remote_task_id") or ledger_state.get("task_id") or ""
    )
    if remote_task_id or (
        not record.first_execution and record.status in {"UNKNOWN", "IN_FLIGHT"}
    ):
        return await _resume_or_recover_creator(
            services=services,
            tool_name=tool_name,
            arguments=arguments,
            context=context,
            credentials=credentials,
            attempt_trace=attempt_trace,
            record_id=record.id,
            operation_key=record.operation_key,
            ledger_state=ledger_state,
            remote_task_id=remote_task_id,
        )

    # Fresh path — revise gate before any Creator submit (claim already held).
    revision_meta: dict[str, Any] = {}
    claim_seed = {}
    if isinstance(record.result, dict) and isinstance(record.result.get("claim"), dict):
        claim_seed = {"claim": dict(record.result["claim"])}
    if tool_name == "creator.revise_draft":
        try:
            await _validate_revise_preconditions(
                services=services,
                arguments=arguments,
                context=context,
                credentials=credentials,
            )
        except LookupError as exc:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=record.operation_key,
                status="FAILED",
                ledger_state=claim_seed or None,
                error=str(exc),
            )
            raise
        revision_meta = {
            "operation_mode": "REVISION",
            "source_draft_id": str(arguments["draft_id"]),
            "expected_content_sha256": str(
                arguments["expected_content_sha256"]
            ).lower(),
            "base_content_sha256": str(
                arguments["expected_content_sha256"]
            ).lower(),
            # Protocol debt: not Creator-native revise.
            "creator_kind": "CREATE_CONTENT",
        }
        if attempt_trace is not None:
            attempt_trace.metadata.update(revision_meta)

    # Persist request fingerprint before remote call.
    ledger_state = {
        "tool_name": tool_name,
        "instruction_hash": stable_hash(str(arguments.get("instruction") or "")),
        "reference_ids": [
            str(item.get("id") or item.get("post_id") or "")
            for item in list(arguments.get("references") or [])
            if item.get("id") or item.get("post_id")
        ],
        "expected_target_role": "CONTENT",
        **claim_seed,
        **revision_meta,
    }
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=record.operation_key,
        ledger_state=ledger_state,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["write_phase"] = "REQUESTING"

    try:
        submitted = await services.creator.submit_draft(
            instruction=str(arguments["instruction"]),
            references=list(arguments.get("references") or []),
            access_token=credentials.access_token,
            idempotency_key=record.operation_key,
            trace_id=credentials.trace_id,
        )
    except Exception as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=record.operation_key,
            status="UNKNOWN",
            ledger_state=ledger_state,
            error=f"creator submit unknown: {exc}",
        )
        raise UnknownSideEffectError(
            f"Creator 提交结果未知，禁止盲目重试：{exc}",
            operation_key=record.operation_key,
        ) from exc

    task_id = str(submitted["task_id"])
    status = str(submitted.get("status") or "QUEUED")
    state = _dependency_state(
        task_id=task_id,
        status=status if status != "QUEUED" else DependencyStatus.PENDING.value,
        submitted_at=_now().isoformat(),
        extra={**claim_seed, **revision_meta},
    )
    await services.ledger.finish(
        run_id=context.run_id,
        operation_key=record.operation_key,
        status="WAITING_DEPENDENCY",
        ledger_state=state,
        remote_operation_id=task_id,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["remote_task_id"] = task_id
        attempt_trace.metadata["dependency_status"] = state["status"]
        attempt_trace.internal_call_count += 1
    _raise_waiting(
        tool_name=tool_name,
        context=context,
        operation_key=record.operation_key,
        side_effect_id=record.id,
        state=state,
    )
    raise AssertionError("unreachable")


async def _resume_or_recover_creator(
    *,
    services: CreatorToolServices,
    tool_name: str,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    credentials: ToolCredentials,
    attempt_trace: ToolAttemptTrace | None,
    record_id: str,
    operation_key: str,
    ledger_state: dict[str, Any],
    remote_task_id: str,
) -> dict[str, Any]:
    state = dict(ledger_state)
    task_id = remote_task_id
    poll_count = int(state.get("poll_count") or 0) + 1
    state["poll_count"] = poll_count
    if attempt_trace is not None:
        attempt_trace.metadata["poll_count"] = poll_count
        attempt_trace.metadata["dependency_status"] = state.get("status")

    if not task_id:
        # UNKNOWN without task_id — strong idempotent recovery submit (once).
        if state.get("idempotency_recovery_attempted"):
            raise UnknownSideEffectError(
                "Creator 任务仍未知，幂等恢复已用尽，等待人工核对",
                operation_key=operation_key,
            )
        if attempt_trace is not None:
            attempt_trace.metadata["idempotency_recovery"] = True
        state["idempotency_recovery_attempted"] = True
        state["idempotency_recovery"] = True
        await services.ledger.mark_in_flight(
            run_id=context.run_id,
            operation_key=operation_key,
            ledger_state=state,
        )
        try:
            submitted = await services.creator.submit_draft(
                instruction=str(arguments["instruction"]),
                references=list(arguments.get("references") or []),
                access_token=credentials.access_token,
                idempotency_key=operation_key,
                trace_id=credentials.trace_id,
            )
        except Exception as exc:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="UNKNOWN",
                ledger_state=state,
                error=f"idempotency recovery failed: {exc}",
            )
            raise UnknownSideEffectError(
                f"Creator 幂等恢复失败，保持 UNKNOWN：{exc}",
                operation_key=operation_key,
            ) from exc
        task_id = str(submitted["task_id"])
        state = _dependency_state(
            task_id=task_id,
            status=str(submitted.get("status") or DependencyStatus.PENDING.value),
            submitted_at=str(state.get("submitted_at") or _now().isoformat()),
            extra={
                "idempotency_recovery": True,
                "idempotency_recovery_attempted": True,
                "poll_count": poll_count,
                **{
                    k: state[k]
                    for k in (
                        "operation_mode",
                        "source_draft_id",
                        "expected_content_sha256",
                    )
                    if k in state
                },
            },
        )
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="WAITING_DEPENDENCY",
            ledger_state=state,
            remote_operation_id=task_id,
        )
        if attempt_trace is not None:
            attempt_trace.metadata["remote_task_id"] = task_id
        _raise_waiting(
            tool_name=tool_name,
            context=context,
            operation_key=operation_key,
            side_effect_id=record_id,
            state=state,
        )

    # Have task_id — poll only.
    if attempt_trace is not None:
        attempt_trace.metadata["remote_task_id"] = task_id
        attempt_trace.internal_call_count += 1

    submitted_at = str(state.get("submitted_at") or _now().isoformat())
    try:
        submitted_dt = datetime.fromisoformat(submitted_at.replace("Z", "+00:00"))
        if submitted_dt.tzinfo is None:
            submitted_dt = submitted_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        submitted_dt = _now()

    # Soft local deadline relative to submit (Creator wall-clock guard).
    # Does not re-submit; keeps WAITING / UNKNOWN.
    creator_timeout = getattr(
        getattr(services.creator, "settings", None),
        "creator_timeout_seconds",
        240,
    )
    if (_now() - submitted_dt).total_seconds() > float(creator_timeout):
        # Still poll once — task may already be terminal.
        pass

    try:
        snapshot = await services.creator.get_task(
            task_id,
            access_token=credentials.access_token,
            trace_id=credentials.trace_id,
        )
    except Exception as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="UNKNOWN",
            ledger_state=state,
            error=f"creator get_task unknown: {exc}",
            remote_operation_id=task_id,
        )
        raise UnknownSideEffectError(
            f"Creator 查询结果未知：{exc}",
            operation_key=operation_key,
        ) from exc

    creator_status = str(snapshot.get("status") or "UNKNOWN")
    if attempt_trace is not None:
        attempt_trace.metadata["dependency_status"] = creator_status

    if creator_status in {"FAILED", "CANCELLED"}:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="FAILED",
            ledger_state={**state, "status": creator_status},
            error=(
                f"Creator task {task_id} ended with {creator_status}: "
                f"{snapshot.get('error_message') or snapshot.get('error_code') or ''}"
            ),
            remote_operation_id=task_id,
        )
        raise RuntimeError(
            f"Creator task {task_id} ended with {creator_status}: "
            f"{snapshot.get('error_message') or snapshot.get('error_code') or ''}"
        )

    if creator_status == "WAITING_HUMAN":
        state.update(
            {
                "status": DependencyStatus.WAITING_HUMAN.value,
                "pending_decision_id": snapshot.get("pending_decision_id"),
                "checkpoint_id": snapshot.get("checkpoint_id"),
                "interrupt_id": snapshot.get("interrupt_id"),
                "required_action": "CREATOR_HUMAN_DECISION",
                "display_message": "创作流程正在等待人工确认",
                "poll_count": poll_count,
            }
        )
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="WAITING_DEPENDENCY",
            ledger_state=state,
            remote_operation_id=task_id,
        )
        _raise_waiting(
            tool_name=tool_name,
            context=context,
            operation_key=operation_key,
            side_effect_id=record_id,
            state=state,
        )

    if creator_status != "COMPLETED":
        mapped = (
            DependencyStatus.PENDING.value
            if creator_status in {"QUEUED", "CREATED"}
            else DependencyStatus.RUNNING.value
        )
        state["status"] = mapped
        state["poll_count"] = poll_count
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="WAITING_DEPENDENCY",
            ledger_state=state,
            remote_operation_id=task_id,
        )
        _raise_waiting(
            tool_name=tool_name,
            context=context,
            operation_key=operation_key,
            side_effect_id=record_id,
            state=state,
        )

    # COMPLETED → handoff (idempotent).
    try:
        handoff = await services.creator.create_handoff(
            task_id=task_id,
            snapshot=snapshot,
            access_token=credentials.access_token,
            idempotency_key=operation_key,
            trace_id=credentials.trace_id,
        )
    except Exception as exc:
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="UNKNOWN",
            ledger_state={**state, "status": "COMPLETED"},
            error=f"creator handoff unknown: {exc}",
            remote_operation_id=task_id,
        )
        raise UnknownSideEffectError(
            f"Creator handoff 结果未知：{exc}",
            operation_key=operation_key,
        ) from exc

    if tool_name == "creator.revise_draft":
        handoff["supersedes_draft_id"] = str(arguments["draft_id"])

    # Strip large body from SideEffect receipt if needed — keep schema fields.
    output = {
        "task_id": str(handoff.get("task_id") or task_id),
        "draft_id": str(handoff["draft_id"]),
        "title": handoff.get("title"),
        "handoff_id": handoff.get("handoff_id"),
        "status": handoff.get("status"),
        "content_sha256": str(handoff["content_sha256"]),
        "description": handoff.get("description"),
        "body_markdown": handoff.get("body_markdown"),
    }
    if handoff.get("supersedes_draft_id"):
        output["supersedes_draft_id"] = str(handoff["supersedes_draft_id"])

    await services.ledger.finish(
        run_id=context.run_id,
        operation_key=operation_key,
        status="COMPLETED",
        output=output,
        ledger_state={
            **state,
            "status": DependencyStatus.COMPLETED.value,
            "draft_id": output["draft_id"],
        },
        remote_operation_id=task_id,
    )
    if attempt_trace is not None:
        attempt_trace.metadata["dependency_status"] = "COMPLETED"
        attempt_trace.metadata["artifact_draft_id"] = output["draft_id"]
        attempt_trace.internal_call_count += 1
    return output


def register_creator_tool_handlers(
    runtime: Any,
    *,
    services: CreatorToolServices,
) -> None:
    async def create_handler(**kwargs: Any) -> dict[str, Any]:
        return await handle_creator_tool(
            services=services, tool_name="creator.create_draft", **kwargs
        )

    async def revise_handler(**kwargs: Any) -> dict[str, Any]:
        return await handle_creator_tool(
            services=services, tool_name="creator.revise_draft", **kwargs
        )

    runtime.register_or_replace_handler("creator.create_draft", create_handler)
    runtime.register_or_replace_handler("creator.revise_draft", revise_handler)
