"""Unified publication.publish_now command for ToolRuntime and Scheduler."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

import httpx

from app.clients import CapabilityGrant, CommunityClient
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
from app.tools import ToolDefinition, ToolRegistry, tool_registry

logger = logging.getLogger(__name__)

IssueCapability = Callable[..., Awaitable[CapabilityGrant]]


class PublishReconcile(StrEnum):
    CONFIRMED_PUBLISHED = "CONFIRMED_PUBLISHED"
    CONFIRMED_NOT_PUBLISHED = "CONFIRMED_NOT_PUBLISHED"
    CONFLICTING_PUBLICATION = "CONFLICTING_PUBLICATION"
    STILL_UNKNOWN = "STILL_UNKNOWN"


@dataclass(frozen=True)
class PublishNowRequest:
    draft_id: str
    expected_content_sha256: str
    creator_id: str
    idempotency_key: str
    capability_token: str
    trace_id: str | None = None
    source: str = "USER"  # USER | SCHEDULER
    run_id: str | None = None


@dataclass(frozen=True)
class PublishNowResult:
    output: dict[str, Any]
    source: str
    idempotency_key_hash: str
    replayed: bool


@dataclass
class PublishNowServices:
    community: CommunityClient
    ledger: SideEffectLedger
    issue_capability: IssueCapability
    registry: ToolRegistry = tool_registry
    consume_budget: Any | None = None


def normalize_publish_output(raw: dict[str, Any], *, draft_id: str) -> dict[str, Any]:
    post_id = str(raw.get("post_id") or raw.get("postId") or raw.get("id") or "")
    status = str(raw.get("status") or "").lower()
    if status == "published":
        status = "published"
    return {
        "post_id": post_id or draft_id,
        "status": status,
        "replayed": bool(raw.get("replayed")),
    }


async def execute_publish_now(
    *,
    community: CommunityClient,
    registry: ToolRegistry,
    request: PublishNowRequest,
) -> PublishNowResult:
    """Single Java publish path shared by ToolRuntime and Scheduler."""

    raw = await community.publish_ai_draft(
        post_id=request.draft_id,
        creator_id=request.creator_id,
        idempotency_key=request.idempotency_key,
        capability_token=request.capability_token,
        expected_content_sha256=request.expected_content_sha256,
        trace_id=request.trace_id,
    )
    validated = registry.validate_output(
        "publication.publish_now",
        raw,
        {
            "draft_id": request.draft_id,
            "expected_content_sha256": request.expected_content_sha256,
        },
        run_id=request.run_id,
    )
    output = normalize_publish_output(validated, draft_id=request.draft_id)
    return PublishNowResult(
        output=output,
        source=request.source,
        idempotency_key_hash=stable_hash(request.idempotency_key),
        replayed=bool(output.get("replayed")),
    )


def _is_http_status(exc: BaseException, *codes: int) -> bool:
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and exc.response is not None
        and exc.response.status_code in codes
    )


def _is_definite_client_failure(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return 400 <= code < 500 and code not in {408, 425, 429}
    if isinstance(exc, (ValueError, LookupError)):
        return True
    return False


async def reconcile_publication_status(
    *,
    community: CommunityClient,
    issue_capability: IssueCapability,
    credentials: ToolCredentials,
    context: ToolInvocationContext,
    draft_id: str,
    expected_content_sha256: str,
) -> tuple[PublishReconcile, dict[str, Any] | None]:
    """Status-based reconcile (Java has no Idempotency-Key store)."""

    expected_sha = expected_content_sha256.lower()
    # Prefer draft read — succeeds only while still a draft.
    try:
        draft_cap = await issue_capability(
            action="community.get_own_draft",
            resources=[f"post:{draft_id}"],
            max_uses=1,
            ttl_seconds=60,
            context=context,
            credentials=credentials,
        )
        draft = await community.get_own_draft(
            draft_id,
            capability_token=draft_cap.token,
            trace_id=credentials.trace_id,
        )
        actual_sha = str(
            draft.get("contentSha256") or draft.get("content_sha256") or ""
        ).lower()
        if actual_sha and actual_sha != expected_sha:
            return PublishReconcile.CONFLICTING_PUBLICATION, {
                "draft_id": draft_id,
                "actual_sha": actual_sha,
            }
        return PublishReconcile.CONFIRMED_NOT_PUBLISHED, {
            "draft_id": draft_id,
            "status": str(draft.get("status") or "draft"),
        }
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in {400, 404, 409}:
            logger.warning("draft reconcile read failed draft_id=%s", draft_id)
            return PublishReconcile.STILL_UNKNOWN, None
    except Exception:
        logger.exception("draft reconcile failed draft_id=%s", draft_id)
        return PublishReconcile.STILL_UNKNOWN, None

    # Draft read failed as expected for published posts — try public get_post.
    try:
        post_cap = await issue_capability(
            action="community.get_post",
            resources=[f"post:{draft_id}"],
            max_uses=1,
            ttl_seconds=60,
            context=context,
            credentials=credentials,
        )
        post = await community.get_post(
            draft_id,
            capability_token=post_cap.token,
            trace_id=credentials.trace_id,
        )
        status = str(post.get("status") or "").lower()
        post_id = str(post.get("id") or post.get("post_id") or draft_id)
        if status == "published" and post_id == draft_id:
            return PublishReconcile.CONFIRMED_PUBLISHED, {
                "post_id": post_id,
                "status": "published",
                "replayed": True,
            }
        return PublishReconcile.CONFLICTING_PUBLICATION, {
            "post_id": post_id,
            "status": status,
        }
    except Exception:
        logger.exception("post reconcile failed draft_id=%s", draft_id)
        return PublishReconcile.STILL_UNKNOWN, None


async def handle_publish_now(
    *,
    services: PublishNowServices,
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
    tool_name = "publication.publish_now"
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
        attempt_trace.metadata["source"] = "USER"
        attempt_trace.metadata["draft_id"] = draft_id
        attempt_trace.metadata["content_sha256"] = expected_sha

    if record.status == "COMPLETED":
        output = completed_output(record)
        if output is None:
            raise RuntimeError("COMPLETED SideEffect missing publish output")
        if attempt_trace is not None:
            attempt_trace.metadata["replayed"] = True
            attempt_trace.metadata["post_id"] = output.get("post_id")
        return {**output, "_runtime_replayed": True}

    if record.first_execution and services.consume_budget is not None:
        await services.consume_budget(context.run_id, "tool")

    ledger_state = ledger_from_record(record)

    # Resume / UNKNOWN: reconcile before any publish.
    if record.status in {"UNKNOWN", "IN_FLIGHT"} and not record.first_execution:
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_attempted"] = True
        outcome, evidence = await reconcile_publication_status(
            community=services.community,
            issue_capability=services.issue_capability,
            credentials=credentials,
            context=context,
            draft_id=draft_id,
            expected_content_sha256=expected_sha,
        )
        if attempt_trace is not None:
            attempt_trace.metadata["reconciliation_result"] = outcome.value
        if outcome == PublishReconcile.CONFIRMED_PUBLISHED and evidence is not None:
            output = {
                "post_id": str(evidence["post_id"]),
                "status": "published",
                "replayed": True,
            }
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="COMPLETED",
                output=output,
                ledger_state={
                    **ledger_state,
                    "reconciliation_result": outcome.value,
                    "idempotency_key_hash": stable_hash(operation_key),
                },
                remote_operation_id=str(evidence["post_id"]),
            )
            if attempt_trace is not None:
                attempt_trace.metadata["post_id"] = output["post_id"]
                attempt_trace.metadata["replayed"] = True
            return {**output, "_runtime_reconciled": True}
        if outcome == PublishReconcile.CONFLICTING_PUBLICATION:
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED",
                ledger_state={
                    **ledger_state,
                    "reconciliation_result": outcome.value,
                    "evidence": evidence,
                },
                error="发布状态与本次操作期望冲突",
            )
            raise LookupError("发布状态与本次操作期望冲突")
        if outcome == PublishReconcile.STILL_UNKNOWN:
            raise UnknownSideEffectError(
                "publication.publish_now 结果仍未知，禁止使用新 key 重试",
                operation_key=operation_key,
            )
        # CONFIRMED_NOT_PUBLISHED → one recovery submit with same key below.

    ledger_state = {
        **ledger_state,
        "draft_id": draft_id,
        "expected_content_sha256": expected_sha,
        "idempotency_key_hash": stable_hash(operation_key),
        "source": "USER",
    }
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=operation_key,
        ledger_state=ledger_state,
    )

    try:
        grant = await services.issue_capability(
            action="publication.publish_now",
            resources=[f"post:{draft_id}"],
            max_uses=1,
            ttl_seconds=120,
            context=context,
            credentials=credentials,
        )
    except Exception as exc:
        if _is_definite_client_failure(exc):
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED",
                ledger_state=ledger_state,
                error=f"capability denied: {exc}",
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
            f"Capability 签发结果未知：{exc}",
            operation_key=operation_key,
        ) from exc

    ledger_state["issued_capability_id"] = grant.capability_id
    await services.ledger.mark_in_flight(
        run_id=context.run_id,
        operation_key=operation_key,
        ledger_state=ledger_state,
    )
    if attempt_trace is not None:
        attempt_trace.internal_call_count += 1
        attempt_trace.metadata["capability_id"] = grant.capability_id

    try:
        result = await execute_publish_now(
            community=services.community,
            registry=services.registry,
            request=PublishNowRequest(
                draft_id=draft_id,
                expected_content_sha256=expected_sha,
                creator_id=context.user_id,
                idempotency_key=operation_key,
                capability_token=grant.token,
                trace_id=credentials.trace_id,
                source="USER",
                run_id=context.run_id,
            ),
        )
    except Exception as exc:
        if attempt_trace is not None:
            attempt_trace.internal_call_count += 1
        if _is_definite_client_failure(exc) and not _is_http_status(
            exc, 401, 403
        ):
            # Business 4xx (not auth) — treat as failed unless auth may mean
            # capability exhausted after a successful publish.
            await services.ledger.finish(
                run_id=context.run_id,
                operation_key=operation_key,
                status="FAILED",
                ledger_state=ledger_state,
                error=str(exc)[:4_000],
            )
            raise
        # 401/403 after authorize may mean exhausted after success — UNKNOWN.
        # Timeouts / 5xx / schema → UNKNOWN; never mint a new idempotency key.
        await services.ledger.finish(
            run_id=context.run_id,
            operation_key=operation_key,
            status="UNKNOWN",
            ledger_state=ledger_state,
            error=str(exc)[:4_000],
        )
        raise UnknownSideEffectError(
            f"发布结果未知，禁止使用新 key 重试：{exc}",
            operation_key=operation_key,
        ) from exc

    if attempt_trace is not None:
        attempt_trace.internal_call_count += 1
        attempt_trace.metadata["post_id"] = result.output.get("post_id")
        attempt_trace.metadata["replayed"] = result.replayed
        attempt_trace.metadata["idempotency_key_hash"] = result.idempotency_key_hash
        assert "token" not in attempt_trace.metadata
        assert "Authorization" not in str(attempt_trace.metadata)

    await services.ledger.finish(
        run_id=context.run_id,
        operation_key=operation_key,
        status="COMPLETED",
        output=result.output,
        ledger_state={
            **ledger_state,
            "idempotency_key_hash": result.idempotency_key_hash,
        },
        remote_operation_id=str(result.output.get("post_id") or draft_id),
    )
    return result.output


def register_publish_now_handler(
    runtime: Any,
    *,
    services: PublishNowServices,
) -> None:
    async def handler(**kwargs: Any) -> dict[str, Any]:
        return await handle_publish_now(services=services, **kwargs)

    runtime.register_or_replace_handler("publication.publish_now", handler)
