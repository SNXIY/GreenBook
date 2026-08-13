"""Agent API routes for conversations, tasks, history, approvals, and events."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from greenbook_agent_core.compatibility.history import RunExecutionAdapter
from greenbook_agent_core.context import PendingApproval, SessionContext
from greenbook_agent_core.conversation import (
    ConversationNotFoundError,
    ExecutionControlCommand,
)
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.human import ApprovalRequestStatus
from greenbook_contracts.identity import AuthContext
from pydantic import BaseModel, Field

from ..models.runtime_result import RuntimeResult
from ..services.conversation_runtime_adapter import ConversationRuntimeAdapter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent")

# ── How session data is stored ───────────────────────────────────────
#
# request.app.state.conversation_store[conv_id] = {
#     "conversation_id": str,
#     "user_id": str,          # frozen — from AuthContext at create time
#     "tenant_id": str,        # frozen — from AuthContext at create time
#     "title": str | None,
#     "created_at": str,       # ISO-8601 UTC
#     "updated_at": str,       # ISO-8601 UTC — bumped on every message
#     "active_draft_id": str | None,
#     "active_schedule_id": str | None,
#     "active_post_id": str | None,
#     "recent_entities": [...],
#     "recent_tool_calls": [...],
#     "pending_approval": ... | None,
#     "last_successful_run_id": str | None,
# }
#
# run_store maps run_id → { run_id, conversation_id, user_id, tenant_id, ... }
# approval_store maps approval_id → { approval_id, user_id, conversation_id, ... }


# ── Request / Response models ────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class ConversationSummary(BaseModel):
    """Public-safe conversation list item.  Never exposes tokens, secrets, or internals."""
    conversation_id: str
    title: str | None = None
    active_draft_id: str | None = None
    active_schedule_id: str | None = None
    created_at: str
    updated_at: str


class ConversationListResponse(BaseModel):
    items: list[ConversationSummary]
    page: int = 1
    size: int = 20
    total: int = 0


class MessageView(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    role: str
    content: str
    parts: list[dict[str, Any]] = Field(default_factory=list)
    run_id: str | None = None
    execution_id: str | None = None
    trace_id: str | None = None
    created_at: str


class MemorySettings(BaseModel):
    episodic_enabled: bool = False
    semantic_enabled: bool = False


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)
    command: dict[str, Any] | None = None


class RunResponse(BaseModel):
    run_id: str
    conversation_id: str
    execution_id: str | None = None
    goal: str = ""
    status: str
    execution_path: str = "ORCHESTRATED"
    workload_lane: str = "WRITE"
    summary: str | None = None
    final_response: str | None = None
    error_code: str | None = None
    error: str | None = None
    trace_id: str | None = None
    budget: dict[str, int] = {}
    timing: dict[str, int | None] = {}
    steps: list[dict[str, object]] = []
    artifacts: list[dict[str, Any]] = []
    partial_results: dict[str, Any] = {}
    approval: object | None = None


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")


def _http_status_for_tool_error(code: str) -> int:
    if code in {"AUTHENTICATION_FAILED", "AUTHENTICATION_REQUIRED"}:
        return status.HTTP_401_UNAUTHORIZED
    if code in {"AUTHORIZATION_DENIED", "PERMISSION_DENIED"}:
        return status.HTTP_403_FORBIDDEN
    if code in {"NOT_FOUND", "RESOURCE_NOT_FOUND"}:
        return status.HTTP_404_NOT_FOUND
    if code in {
        "CONFLICT", "IDEMPOTENCY_CONFLICT", "DRAFT_VERSION_CONFLICT",
    }:
        return status.HTTP_409_CONFLICT
    if code in {
        "VALIDATION_ERROR",
        "INVALID_REQUEST",
        "INVALID_TOOL_ARGUMENT",
        "TOOL_ARGUMENT_VALIDATION_FAILED",
        "PRE_EXECUTION_VALIDATION_FAILED",
    }:
        return status.HTTP_400_BAD_REQUEST
    if code == "DOWNSTREAM_VALIDATION_FAILED":
        return status.HTTP_422_UNPROCESSABLE_ENTITY
    if code in {"JAVA_BACKEND_UNAVAILABLE", "CREATOR_UNAVAILABLE", "DEPENDENCY_UNAVAILABLE"}:
        return status.HTTP_503_SERVICE_UNAVAILABLE
    if code in {"BUSINESS_REJECTED", "SCHEDULE_NOT_MODIFIABLE"}:
        return status.HTTP_409_CONFLICT
    if code == "TIMEOUT":
        return status.HTTP_504_GATEWAY_TIMEOUT
    return status.HTTP_502_BAD_GATEWAY




# ── Helpers ──────────────────────────────────────────────────────

def _get_auth(request: Request) -> AuthContext:
    auth = getattr(request.state, "auth_context", None)
    if auth is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return auth


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _conversation_belongs_to(auth: AuthContext, conversation_data: dict[str, Any]) -> bool:
    return (
        str(conversation_data.get("user_id", "")) == auth.user_id
        and str(conversation_data.get("tenant_id", "")) == auth.tenant_id
    )


async def _get_session(request: Request, conversation_id: str) -> SessionContext:
    """Get or create a SessionContext, verifying ownership on existing sessions."""
    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        try:
            snapshot = await conversation_service.load(
                conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Conversation not found",
            ) from exc
        return snapshot.session

    store = request.app.state.conversation_store

    if conversation_id in store:
        data = store[conversation_id]
        if not _conversation_belongs_to(auth, data):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return SessionContext(**data)

    # Conversations are created explicitly by POST /conversations.  A guessed
    # ID must never create an implicit resource or become a side channel.
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Conversation not found",
    )


async def _save_session(request: Request, session: SessionContext) -> None:
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        await conversation_service.save_session(session)
        return
    store = request.app.state.conversation_store
    existing = store.get(session.conversation_id, {})
    store[session.conversation_id] = {
        **session.model_dump(mode="json"),
        "title": existing.get("title"),
        "created_at": existing.get("created_at", _now_iso()),
        "updated_at": _now_iso(),
    }


async def _prepare_message_history(
    request: Request,
    *,
    conversation_id: str,
    auth: AuthContext,
    content: str,
    trace_id: str,
) -> list[dict[str, str]]:
    """Load compressed durable context, then append the current user turn."""

    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        snapshot = await conversation_service.load(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        history = snapshot.history_for_model()
        await conversation_service.append_message(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            role="user",
            content=content,
            trace_id=trace_id,
        )
        return history

    msg_store = request.app.state.message_store
    history = [
        {"role": item["role"], "content": item["content"]}
        for item in msg_store.get(conversation_id, [])
        if item.get("role") in {"user", "assistant"}
    ]
    msg_store.setdefault(conversation_id, []).append({
        "role": "user",
        "content": content,
        "created_at": _now_iso(),
    })
    return history


async def _append_agent_message(
    request: Request,
    *,
    conversation_id: str,
    auth: AuthContext,
    content: str,
    trace_id: str,
    parts: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    execution_id: str | None = None,
) -> None:
    if not content:
        return
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        await conversation_service.append_message(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            role="assistant",
            content=content,
            trace_id=trace_id,
            parts=parts,
            run_id=run_id,
            execution_id=execution_id,
        )
        return
    request.app.state.message_store.setdefault(conversation_id, []).append({
        "role": "assistant",
        "content": content,
        "trace_id": trace_id,
        "parts": list(parts or []),
        "created_at": _now_iso(),
    })


def _conversation_summary(data: dict[str, Any]) -> ConversationSummary:
    return ConversationSummary(
        conversation_id=str(data.get("conversation_id", "")),
        title=data.get("title"),
        active_draft_id=data.get("active_draft_id"),
        active_schedule_id=data.get("active_schedule_id"),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )




def _auth_store_get(store_key: str, request: Request, record_id: str) -> dict[str, Any]:
    """Get a stored record, verifying it belongs to the authenticated user."""
    auth = _get_auth(request)
    store = getattr(request.app.state, store_key)
    if record_id not in store:
        raise HTTPException(status_code=404, detail="Record not found")
    record = store[record_id]
    if not _conversation_belongs_to(auth, record):
        raise HTTPException(status_code=404, detail="Record not found")
    return record


def _run_record_from_projection(
    projection: Any,
    *,
    user_id: str,
    tenant_id: str,
) -> dict[str, Any]:
    response = projection.assistant_response or {}
    return {
        "run_id": projection.run_id,
        "conversation_id": projection.conversation_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "status": projection.status,
        "content": response.get("message") or projection.summary,
        "trace_id": projection.trace_id,
        "execution_id": projection.execution_id,
        "task_id": projection.task_id,
        "steps": list(response.get("steps") or []),
        "artifacts": list(projection.artifacts),
        "presentation": response,
        "error_code": response.get("error_code"),
        "error": response.get("error") or response.get("error_message"),
    }


async def _durable_run_record(
    run_id: str,
    request: Request,
    auth: AuthContext,
) -> dict[str, Any] | None:
    store = getattr(request.app.state, "execution_result_projection_store", None)
    getter = getattr(store, "get_by_run_id", None)
    if not callable(getter):
        return None
    projection = getter(run_id)
    if projection is None:
        return None

    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        conversation = await conversation_service.get_conversation(
            projection.conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        if conversation is None:
            return None
    else:
        conversation = request.app.state.conversation_store.get(
            projection.conversation_id
        )
        if conversation is None or not _conversation_belongs_to(auth, conversation):
            return None
    return _run_record_from_projection(
        projection,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
    )


# ── Tool schema for LLM ─────────────────────────────────────────


@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> ConversationListResponse:
    """List conversations for the authenticated user, newest first."""
    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        owned = await conversation_service.list_conversations(
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        total = len(owned)
        start = (page - 1) * size
        page_items = owned[start : start + size]
        return ConversationListResponse(
            items=[_conversation_summary(item) for item in page_items],
            page=page,
            size=size,
            total=total,
        )
    store = request.app.state.conversation_store

    # Filter and sort
    owned: list[dict[str, Any]] = [
        data for data in store.values()
        if _conversation_belongs_to(auth, data)
    ]
    owned.sort(
        key=lambda d: d.get("updated_at") or d.get("created_at") or "",
        reverse=True,
    )

    total = len(owned)
    start = (page - 1) * size
    page_items = owned[start : start + size]

    return ConversationListResponse(
        items=[_conversation_summary(item) for item in page_items],
        page=page,
        size=size,
        total=total,
    )


@router.post("/conversations", response_model=ConversationSummary)
async def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
) -> ConversationSummary:
    auth = _get_auth(request)
    conversation_id = str(uuid.uuid4())
    now = _now_iso()
    session = SessionContext(
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        timezone=auth.timezone,
    )
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        record = await conversation_service.create_conversation(
            conversation_id=conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            title=body.title,
            timezone=auth.timezone,
        )
        return _conversation_summary(record)

    store = request.app.state.conversation_store
    store[conversation_id] = {
        **session.model_dump(mode="json"),
        "title": body.title,
        "created_at": now,
        "updated_at": now,
    }
    return _conversation_summary(store[conversation_id])


@router.get("/conversations/{conversation_id}/tasks")
async def list_conversation_tasks(conversation_id: str, request: Request):
    """Return the structured Task/Goal/Execution index for one conversation."""
    auth = _get_auth(request)
    session = await _get_session(request, conversation_id)
    if session.user_id != auth.user_id or session.tenant_id != auth.tenant_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Conversation scope mismatch")
    adapter = _conversation_runtime_adapter(request)
    return {
        "conversation_id": conversation_id,
        "items": await adapter.get_task_index(
            conversation_id=conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        ),
    }


class RunAcceptedResponse(BaseModel):
    run_id: str
    conversation_id: str
    status: str
    events_url: str
    execution_id: str | None = None
    execution_ids: list[str] = []
    task_ids: list[str] = []
    execution_events_url: str | None = None
    error_code: str | None = None
    error: str | None = None
    replayed: bool = False


def _conversation_runtime_adapter(request: Request) -> ConversationRuntimeAdapter:
    """Return the lifespan-wired adapter, with a test-safe lazy fallback."""
    adapter = getattr(request.app.state, "conversation_runtime_adapter", None)
    if adapter is None:
        adapter = ConversationRuntimeAdapter(
            runtime_service=getattr(request.app.state, "runtime_agent_service", None),
            execution_repository=getattr(
                request.app.state, "execution_repository", None,
            ),
        )
        request.app.state.conversation_runtime_adapter = adapter
    return adapter


def _runtime_events(result: RuntimeResult, run_id: str) -> list[dict[str, Any]]:
    """Project Runtime events into the legacy run-event envelope."""
    projected: list[dict[str, Any]] = []
    for raw in result.events:
        if isinstance(raw, dict):
            event_type = raw.get("event") or raw.get("event_type") or "RUNTIME_EVENT"
            payload = raw.get("data")
            if not isinstance(payload, dict):
                payload = raw.get("payload")
            if not isinstance(payload, dict):
                payload = {}
        else:
            event_type = getattr(raw, "event_type", "RUNTIME_EVENT")
            event_type = getattr(event_type, "value", event_type)
            payload = getattr(raw, "payload", {})
            if not isinstance(payload, dict):
                payload = {}
        projected.append({
            "event": str(event_type),
            "data": {"run_id": run_id, **payload},
        })

    if projected:
        return projected

    terminal_event = (
        "RUN_COMPLETED" if result.status == "COMPLETED"
        else "RUN_FAILED" if result.status == "FAILED"
        else "RUN_STARTED"
    )
    data: dict[str, Any] = {"run_id": run_id, "status": result.status}
    if result.error_code:
        data["code"] = result.error_code
    if result.error_message:
        data["message"] = result.error_message
    return [{"event": terminal_event, "data": data}]


def _runtime_run_record(
    result: RuntimeResult,
    *,
    run_id: str,
    conversation_id: str,
    user_id: str,
    tenant_id: str,
    trace_id: str,
) -> dict[str, Any]:
    """Create a compatibility projection; Runtime remains canonical state."""
    return {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "status": result.status,
        "content": result.content,
        "error_code": result.error_code or None,
        "error": result.error_message or result.error or None,
        "trace_id": trace_id,
        "tool_rounds": result.tool_rounds,
        "events": _runtime_events(result, run_id),
        "execution_id": result.execution_id,
        "execution_ids": list((result.partial_results or {}).get("execution_ids", [])),
        "task_ids": list((result.partial_results or {}).get("task_ids", [])),
        "partial_results": result.partial_results or {},
        "agent_timeline": list((result.partial_results or {}).get("nodes", [])),
        "plan_id": result.plan_id,
        "task_id": result.task_id,
        "steps": list(result.steps),
        "artifacts": list(result.artifacts),
        "approval_id": result.approval_id,
    }


async def _send_runtime_message(
    conversation_id: str,
    body: MessageCreateRequest,
    request: Request,
    auth: AuthContext,
    session: SessionContext,
) -> RunAcceptedResponse:
    """Run the message through Runtime while preserving the old response IDs."""
    if body.timezone:
        session.timezone = body.timezone

    run_id = str(uuid.uuid4())
    trace_id = str(uuid.uuid4())
    conversation_history = await _prepare_message_history(
        request,
        conversation_id=conversation_id,
        auth=auth,
        content=body.content,
        trace_id=trace_id,
    )

    adapter = _conversation_runtime_adapter(request)
    try:
        command_override = (
            ExecutionControlCommand.model_validate(body.command)
            if body.command is not None
            else None
        )
        result = await adapter.execute(
            conversation_id=conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
            message=body.content,
            history=conversation_history,
            session=session,
            timezone=session.timezone,
            run_id=run_id,
            trace_id=trace_id,
            mcp=getattr(request.app.state, "mcp", None),
            llm=getattr(request.app.state, "llm", None),
            model=getattr(request.app.state, "model", ""),
            auth=auth,
            _command_override=command_override,
        )
    except Exception as exc:
        logger.exception("Runtime message adapter failed run_id=%s", run_id)
        result = RuntimeResult(
            success=False,
            status="FAILED",
            run_id=run_id,
            execution_path="runtime",
            error_code="RUNTIME_ADAPTER_FAILED",
            error_message=str(exc) or "Runtime adapter failed",
            trace_id=trace_id,
        )

    if not result.status:
        result.status = "COMPLETED" if result.success else "FAILED"
    if not result.run_id:
        result.run_id = run_id
    if not result.trace_id:
        result.trace_id = trace_id

    logger.warning(
        "Agent runtime result run_id=%s status=%s error_code=%s execution_id=%s error=%s",
        run_id,
        result.status,
        result.error_code or "",
        result.execution_id or "",
        result.error_message or result.error or "",
    )

    if result.execution_id:
        link_adapter = getattr(request.app.state, "run_execution_adapter", None)
        if link_adapter is None:
            link_adapter = RunExecutionAdapter()
            request.app.state.run_execution_adapter = link_adapter
        link_adapter.bind_run_execution(
            run_id,
            result.execution_id,
            conversation_id=conversation_id,
            task_id=result.task_id,
        )

    approval_service = getattr(request.app.state, "approval_runtime_service", None)
    if approval_service is not None:
        approval = await approval_service.capture_result(
            result,
            conversation_id=conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        if approval is not None:
            session.pending_approval = PendingApproval(
                approval_id=approval.approval_id,
                operation=approval.operation,
                resource_id=approval.resource_id,
                description=approval.message,
            )
    await _save_session(request, session)
    projected_terminal_result = False
    completion_publisher = getattr(
        request.app.state,
        "execution_completion_publisher",
        None,
    )
    if (
        completion_publisher is not None
        and result.execution_id
        and result.status in {"COMPLETED", "FAILED", "CANCELLED"}
    ):
        await completion_publisher(
            ExecutionQueueMessage(
                execution_id=result.execution_id,
                trace_id=trace_id,
                payload={
                    "run_id": run_id,
                    "conversation_id": conversation_id,
                    "task_id": result.task_id,
                    "user_message": body.content,
                    "timezone": session.timezone,
                    "auth_context": {
                        "user_id": auth.user_id,
                        "tenant_id": auth.tenant_id,
                        "roles": list(auth.roles),
                        "timezone": session.timezone,
                    },
                },
            ),
            result,
            auth,
        )
        projected_terminal_result = True
    if not projected_terminal_result:
        clarification = (result.partial_results or {}).get("clarification")
        message_parts: list[dict[str, Any]] = []
        if isinstance(clarification, dict):
            message_parts.append(clarification)
        policy_decision = (result.partial_results or {}).get("policy_decision")
        audit_event = (result.partial_results or {}).get("audit_event")
        if isinstance(policy_decision, dict) or isinstance(audit_event, dict):
            message_parts.append({
                "type": "policy_decision",
                "policy_decision": policy_decision or {},
                "audit_event": audit_event or {},
            })
        await _append_agent_message(
            request,
            conversation_id=conversation_id,
            auth=auth,
            content=result.content,
            trace_id=trace_id,
            parts=message_parts or None,
            run_id=run_id,
            execution_id=result.execution_id,
        )
    if result.status == "COMPLETED":
        session.last_successful_run_id = run_id
        await _save_session(request, session)

    request.app.state.run_store[run_id] = _runtime_run_record(
        result,
        run_id=run_id,
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        trace_id=trace_id,
    )

    return RunAcceptedResponse(
        run_id=run_id,
        conversation_id=conversation_id,
        status=result.status,
        events_url=(
            f"/api/v1/agent/executions/{result.execution_id}/events"
            if result.execution_id
            else f"/api/v1/agent/runs/{run_id}"
        ),
        execution_id=result.execution_id,
        execution_ids=list((result.partial_results or {}).get("execution_ids", [])),
        task_ids=list((result.partial_results or {}).get("task_ids", [])),
        execution_events_url=(
            f"/api/v1/agent/executions/{result.execution_id}/events"
            if result.execution_id else None
        ),
        error_code=result.error_code or None,
        error=result.error_message or result.error or None,
    )


@router.post(
    "/conversations/{conversation_id}/messages",
    response_model=RunAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def send_message(
    conversation_id: str,
    body: MessageCreateRequest,
    request: Request,
):
    auth = _get_auth(request)
    session = await _get_session(request, conversation_id)
    return await _send_runtime_message(
        conversation_id,
        body,
        request,
        auth,
        session,
    )



@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str, request: Request) -> list[MessageView]:
    """Get message history for a conversation (ownership-verified)."""
    auth = _get_auth(request)
    conversation_service = getattr(request.app.state, "conversation_service", None)
    if conversation_service is not None:
        try:
            messages = await conversation_service.list_messages(
                conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="Conversation not found") from exc
        return [
            MessageView(
                message_id=item.get("message_id", str(uuid.uuid4())),
                role=item["role"],
                content=item["content"],
                parts=list(item.get("parts") or []),
                run_id=item.get("run_id"),
                execution_id=item.get("execution_id"),
                trace_id=item.get("trace_id"),
                created_at=item.get("created_at", ""),
            )
            for item in messages
        ]

    store = request.app.state.conversation_store
    if conversation_id not in store:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not _conversation_belongs_to(auth, store[conversation_id]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg_store = request.app.state.message_store
    return [
        MessageView(message_id=m.get("message_id", str(uuid.uuid4())),
                    role=m["role"], content=m["content"],
                    trace_id=m.get("trace_id"), created_at=m.get("created_at", ""))
        for m in msg_store.get(conversation_id, [])
    ]


@router.get("/memories")
async def get_memories(request: Request) -> dict[str, object]:
    _get_auth(request)
    return {"memories": []}


@router.get("/memory/episodes")
async def get_episodes(request: Request, limit: int = 10) -> dict[str, object]:
    _get_auth(request)
    return {"episodes": []}


@router.get("/memory/settings")
async def get_memory_settings(request: Request) -> MemorySettings:
    _get_auth(request)
    return MemorySettings()


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> RunResponse:
    auth = _get_auth(request)
    record = request.app.state.run_store.get(run_id)
    if record is not None and not _conversation_belongs_to(auth, record):
        # Startup reconciliation can restore a projection into the process
        # local run store without identity fields. Rebuild it from the
        # durable projection before applying the ownership decision.
        record = await _durable_run_record(run_id, request, auth)
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        request.app.state.run_store[run_id] = record
    if record is None:
        record = await _durable_run_record(run_id, request, auth)
        if record is None:
            raise HTTPException(status_code=404, detail="Record not found")
        request.app.state.run_store[run_id] = record

    projection_store = getattr(request.app.state, "execution_result_projection_store", None)
    projection = (
        projection_store.get(record.get("execution_id"))
        if projection_store is not None and record.get("execution_id")
        else None
    )
    if projection is not None and projection.run_id == run_id:
        record.update(
            _run_record_from_projection(
                projection,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        )
        request.app.state.run_store[run_id] = record
    events = record.get("events", [])
    steps: list[dict[str, object]] = list(record.get("steps", []))
    for i, evt in enumerate(events):
        if steps:
            break
        if evt.get("event") in ("TOOL_CALL_STARTED", "TOOL_CALL_COMPLETED", "TOOL_CALL_FAILED"):
            data = evt.get("data", {})
            event_name = evt.get("event")
            steps.append({
                "step_id": data.get("tool_call_id", f"step-{i}"),
                "ordinal": i + 1,
                "kind": "TOOL",
                "tool_name": data.get("tool_name", ""),
                "label": data.get("tool_name", f"Step {i+1}"),
                "status": (
                    "RUNNING" if event_name == "TOOL_CALL_STARTED"
                    else "COMPLETED" if data.get("ok") else "FAILED"
                ),
                "output": data,
            })
    content = record.get("content", "")
    approval = None
    approval_id = record.get("approval_id")
    durable_approvals = getattr(request.app.state, "approval_runtime_service", None)
    if not approval_id and durable_approvals is not None and record.get("execution_id"):
        pending = await durable_approvals.get_for_execution(record["execution_id"])
        if pending is not None:
            approval_id = pending.approval_id
            record["approval_id"] = approval_id
    if approval_id:
        approval_record = None
        if durable_approvals is not None:
            approval_record = await durable_approvals.get_request(approval_id)
        if approval_record is not None:
            auth = _get_auth(request)
            if (
                approval_record.user_id == auth.user_id
                and approval_record.tenant_id == auth.tenant_id
            ):
                approval = {
                    "approval_id": approval_id,
                    "action": approval_record.operation,
                    "status": approval_record.status.value,
                    "description": approval_record.message,
                    "preview": {"resource_id": approval_record.resource_id},
                    "expires_at": "",
                    "expected_run_version": 0,
                }
        else:
            approval_record = request.app.state.approval_store.get(approval_id)
        if (
            approval is None
            and isinstance(approval_record, dict)
            and _conversation_belongs_to(_get_auth(request), approval_record)
        ):
            approval = {
                "approval_id": approval_id,
                "action": approval_record.get("operation", ""),
                "status": approval_record.get("status", "PENDING"),
                "description": approval_record.get("description", ""),
                "preview": {"resource_id": approval_record.get("resource_id")},
                "expires_at": approval_record.get("expires_at", ""),
                "expected_run_version": 0,
            }
    return RunResponse(
        run_id=record["run_id"],
        conversation_id=record["conversation_id"],
        execution_id=record.get("execution_id"),
        goal=content[:120] if content else "",
        status=record["status"],
        summary=content[:200] if content else None,
        final_response=content,
        error_code=record.get("error_code"),
        error=record.get("error"),
        trace_id=record.get("trace_id"),
        budget={
            "model_calls": 1, "max_model_calls": 6,
            "tool_calls": record.get("tool_rounds", 0), "max_tool_calls": 30,
            "replan_count": 0, "max_replans": 0,
        },
        timing={"queue_ms": None, "model_ms": 0, "tool_ms": 0, "dependency_wait_ms": 0, "total_ms": None},
        steps=steps,
        artifacts=list(record.get("artifacts", [])),
        partial_results=dict(record.get("partial_results", {})),
        approval=approval,
    )


@router.get("/runs")
async def list_runs(request: Request, limit: int = 30) -> list[RunResponse]:
    auth = _get_auth(request)
    run_store = request.app.state.run_store
    owned: list[dict[str, Any]] = []
    for record in list(run_store.values()):
        if _conversation_belongs_to(auth, record):
            owned.append(record)
            continue
        run_id = str(record.get("run_id") or "")
        if not run_id:
            continue
        projected = await _durable_run_record(run_id, request, auth)
        if projected is not None:
            run_store[run_id] = projected
            owned.append(projected)
    owned.sort(key=lambda r: r.get("trace_id", ""), reverse=True)
    return [
        RunResponse(
            run_id=r["run_id"], conversation_id=r["conversation_id"],
            execution_id=r.get("execution_id"),
            goal="", status=r["status"],
            summary=(r.get("content") or "")[:200],
            final_response=r.get("content"),
            error_code=r.get("error_code"),
            error=r.get("error"),
            trace_id=r.get("trace_id"),
            budget={"model_calls": 1, "max_model_calls": 6, "tool_calls": r.get("tool_rounds", 0), "max_tool_calls": 30, "replan_count": 0, "max_replans": 0},
            timing={"queue_ms": None, "model_ms": 0, "tool_ms": 0, "dependency_wait_ms": 0, "total_ms": None},
            steps=[],
            artifacts=list(r.get("artifacts", [])),
            partial_results=dict(r.get("partial_results", {})),
        )
        for r in owned[:limit]
    ]


@router.post("/approvals/{approval_id}/approve")
async def approve_operation(
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    record = _auth_store_get("approval_store", request, approval_id)

    if body.decision != "APPROVE":
        record["status"] = "REJECTED"
        return {"approval_id": approval_id, "status": "REJECTED"}

    record["status"] = "APPROVED"
    conv_id = record.get("conversation_id", "")
    if conv_id in request.app.state.conversation_store:
        session_data = request.app.state.conversation_store[conv_id]
        session = SessionContext(**session_data)
        session.pending_approval = None
        request.app.state.conversation_store[conv_id] = {
            **session.model_dump(mode="json"),
            "title": session_data.get("title"),
            "created_at": session_data.get("created_at", _now_iso()),
            "updated_at": _now_iso(),
        }

    return {"approval_id": approval_id, "status": "APPROVED"}


@router.post("/executions/{execution_id}/approve")
async def approve_execution(
    execution_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
) -> dict[str, Any]:
    """Approve the pending step attached to a canonical Execution."""

    service = getattr(request.app.state, "approval_runtime_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Durable approval service unavailable")
    pending = await service.get_for_execution(execution_id)
    if pending is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    auth = _get_auth(request)
    try:
        result = await service.decide(
            pending.approval_id,
            decision=(
                ApprovalRequestStatus.APPROVED
                if body.decision == "APPROVE"
                else ApprovalRequestStatus.REJECTED
            ),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Approval request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "execution_id": execution_id,
        "approval_id": pending.approval_id,
        "status": result.status,
    }


@router.post("/runs/{run_id}/approvals/{approval_id}")
async def decide_runtime_approval(
    run_id: str,
    approval_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
) -> RunResponse:
    record = _auth_store_get("run_store", request, run_id)
    service = getattr(request.app.state, "approval_runtime_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Durable approval service unavailable")
    auth = _get_auth(request)
    try:
        result = await service.decide(
            approval_id,
            decision=(
                ApprovalRequestStatus.APPROVED
                if body.decision == "APPROVE"
                else ApprovalRequestStatus.REJECTED
            ),
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail="Approval request not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    record.update({
        "status": result.status,
        "content": result.content,
        "execution_id": result.execution_id or record.get("execution_id"),
    })
    request.app.state.run_store[run_id] = record
    return await get_run(run_id, request)


@router.post("/approvals/{approval_id}/reject")
async def reject_operation(
    approval_id: str,
    request: Request,
) -> dict[str, Any]:
    record = _auth_store_get("approval_store", request, approval_id)
    record["status"] = "REJECTED"
    return {"approval_id": approval_id, "status": "REJECTED"}


# ── Helpers ──────────────────────────────────────────────────────

async def _sse_stream(events: Any):
    for event in events:
        event_type = event.get("event", "message")
        data = json.dumps(event.get("data", {}), ensure_ascii=False, default=str)
        yield f"event: {event_type}\ndata: {data}\n\n"
    yield "event: done\ndata: {}\n\n"
