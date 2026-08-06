"""Assistant API routes — conversations, messages, runs, approvals, SSE events."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from greenbook_assistant_core.agent import CommunityOperationsAssistant
from greenbook_assistant_core.context import PendingApproval, SessionContext
from greenbook_assistant_core.time_parser import (
    format_local_schedule_time,
    parse_natural_schedule_time,
)
from greenbook_contracts.identity import AuthContext
from greenbook_mcp_server.tool_schemas import (
    ReviseDraftArguments,
    UpdateScheduleArguments,
    openai_parameters,
)
from greenbook_security.policy import requires_approval
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant")

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
    role: str
    content: str
    trace_id: str | None = None
    created_at: str


class MemorySettings(BaseModel):
    episodic_enabled: bool = False
    semantic_enabled: bool = False


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


class RunResponse(BaseModel):
    run_id: str
    conversation_id: str
    goal: str = ""
    status: str
    execution_path: str = "ORCHESTRATED"
    workload_lane: str = "WRITE"
    intent: str | None = None
    summary: str | None = None
    final_response: str | None = None
    error_code: str | None = None
    error: str | None = None
    trace_id: str | None = None
    budget: dict[str, int] = {}
    timing: dict[str, int | None] = {}
    steps: list[dict[str, object]] = []
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


def _normalize_schedule_tool_args(
    tool_args: dict[str, Any],
    *,
    user_message: str,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize a model schedule call to the Assistant→Java contract."""
    normalized = dict(tool_args)
    normalized.pop("timezone_name", None)
    parsed_run_at = parse_natural_schedule_time(
        user_message,
        timezone_name,
        now=now,
    )
    if parsed_run_at:
        normalized["run_at"] = parsed_run_at
    normalized["timezone"] = timezone_name
    return normalized


def _normalize_update_schedule_tool_args(
    tool_args: dict[str, Any],
    *,
    user_message: str,
    timezone_name: str,
    now: datetime,
) -> dict[str, Any]:
    """Own relative update times deterministically at message receipt time."""
    normalized = dict(tool_args)
    # ``publish_at`` was emitted by an older planner.  Convert that one
    # compatibility alias before applying the deterministic relative-time
    # value.  If both names are present, retain both so the MCP Pydantic
    # boundary can reject a conflicting pair instead of guessing.
    if "publish_at" in normalized and "run_at" not in normalized:
        normalized["run_at"] = normalized.pop("publish_at")
    parsed_run_at = parse_natural_schedule_time(
        user_message,
        timezone_name,
        now=now,
    )
    if parsed_run_at:
        normalized["run_at"] = parsed_run_at
    return normalized


def _bind_target_tool_args(
    tool_name: str,
    tool_args: dict[str, Any],
    session: SessionContext,
) -> dict[str, Any]:
    """Bind omitted targets to the conversation's explicit active resources."""
    normalized = dict(tool_args)
    if (
        tool_name == "content.revise_draft"
        and not normalized.get("draft_id")
        and session.active_draft_id
    ):
        normalized["draft_id"] = session.active_draft_id
    if (
        tool_name == "publication.update_schedule"
        and not normalized.get("schedule_id")
        and session.active_schedule_id
    ):
        normalized["schedule_id"] = session.active_schedule_id
    return normalized


def _append_schedule_confirmation(
    content: str,
    *,
    draft: dict[str, Any] | None,
    schedule: dict[str, Any] | None,
) -> str:
    """Ensure the frontend receives a stable, user-readable write summary."""
    if not schedule:
        return content

    draft_id = str((draft or {}).get("draft_id") or schedule.get("draft_id") or "")
    schedule_id = str(schedule.get("schedule_id") or "")
    title = str((draft or {}).get("title") or "未命名草稿")
    run_at = str(schedule.get("run_at") or "")
    timezone_name = str(schedule.get("timezone") or "Asia/Shanghai")
    local_time = format_local_schedule_time(run_at, timezone_name)
    required = (draft_id, schedule_id, local_time, timezone_name, "SCHEDULED")
    if all(value and value in content for value in required):
        return content

    confirmation = "\n".join([
        "执行结果：",
        f"标题：{title}",
        f"draftId：{draft_id}",
        f"scheduleId：{schedule_id}",
        f"发布时间：{local_time}",
        f"时区：{timezone_name}",
        "当前状态：SCHEDULED",
    ])
    return f"{content.rstrip()}\n\n{confirmation}".strip()


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


def _get_session(request: Request, conversation_id: str) -> SessionContext:
    """Get or create a SessionContext, verifying ownership on existing sessions."""
    auth = _get_auth(request)
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


def _save_session(request: Request, session: SessionContext) -> None:
    store = request.app.state.conversation_store
    existing = store.get(session.conversation_id, {})
    store[session.conversation_id] = {
        **session.model_dump(mode="json"),
        "title": existing.get("title"),
        "created_at": existing.get("created_at", _now_iso()),
        "updated_at": _now_iso(),
    }


def _conversation_summary(data: dict[str, Any]) -> ConversationSummary:
    return ConversationSummary(
        conversation_id=str(data.get("conversation_id", "")),
        title=data.get("title"),
        active_draft_id=data.get("active_draft_id"),
        active_schedule_id=data.get("active_schedule_id"),
        created_at=str(data.get("created_at", "")),
        updated_at=str(data.get("updated_at", "")),
    )


def _auth_store_put(store_key: str, request: Request, record_id: str, record: dict[str, Any]) -> None:
    """Store a record with ownership fields from AuthContext."""
    auth = _get_auth(request)
    store = getattr(request.app.state, store_key)
    store[record_id] = {
        **record,
        "user_id": auth.user_id,
        "tenant_id": auth.tenant_id,
    }


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


# ── Tool schema for LLM ─────────────────────────────────────────

def _build_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "community_search_public_posts",
                "description": "Search public posts in the GreenBook community by keywords. Returns matching posts with titles, summaries, and engagement stats.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search keywords or topic"},
                        "sort": {"type": "string", "enum": ["hot", "latest", "relevant"], "default": "latest"},
                        "page": {"type": "integer", "default": 1},
                        "size": {"type": "integer", "default": 20},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "community_get_post",
                "description": "Get full details of a single post by ID, including body content",
                "parameters": {
                    "type": "object",
                    "properties": {"post_id": {"type": "string", "description": "Post ID to retrieve"}},
                    "required": ["post_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "community_list_own_posts",
                "description": "List the current logged-in user's own published posts",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "page": {"type": "integer", "default": 1},
                        "size": {"type": "integer", "default": 20},
                    },
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "content_create_draft",
                "description": "Create a new draft post. The system will use AI to generate content based on your instructions and any search references from this conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Draft title (can be auto-generated)"},
                        "instruction": {"type": "string", "description": "Content instruction: topic, key points, style, target audience, etc."},
                    },
                    "required": ["title", "instruction"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "content_get_draft",
                "description": "Get a draft by ID. If no ID provided, resolves the most recent draft from this conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {"draft_id": {"type": "string", "description": "Draft ID (optional, uses active draft if omitted)"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "content_list_drafts",
                "description": "List the current user's drafts",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "content_revise_draft",
                "description": "Revise an existing draft. The system will regenerate content based on your revision instructions. If no draft_id specified, revises the most recent draft from this conversation.",
                "parameters": openai_parameters(ReviseDraftArguments),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication_schedule",
                "description": "Schedule a draft for future publication at a specific date and time.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "draft_id": {"type": "string", "description": "Draft ID to schedule (optional, uses active draft if omitted)"},
                        "run_at": {"type": "string", "description": "ISO-8601 datetime with timezone for when to publish, e.g. 2026-08-07T09:00:00+08:00"},
                        "timezone": {"type": "string", "description": "Timezone name, e.g. Asia/Shanghai", "default": "Asia/Shanghai"},
                    },
                    "required": ["run_at"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication_get_status",
                "description": "Check the status of a scheduled publication",
                "parameters": {
                    "type": "object",
                    "properties": {"schedule_id": {"type": "string", "description": "Schedule ID (optional, uses active schedule if omitted)"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication_update_schedule",
                "description": "Change the scheduled publication time. Only works for schedules in SCHEDULED status.",
                "parameters": openai_parameters(UpdateScheduleArguments),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication_cancel_schedule",
                "description": "Cancel a scheduled publication. Can only cancel SCHEDULED tasks.",
                "parameters": {
                    "type": "object",
                    "properties": {"schedule_id": {"type": "string", "description": "Schedule ID (optional, uses active if omitted)"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication_publish_now",
                "description": "Publish a draft immediately. REQUIRES user confirmation/approval before executing.",
                "parameters": {
                    "type": "object",
                    "properties": {"draft_id": {"type": "string", "description": "Draft ID (optional)"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "interaction_list_comments",
                "description": "List comments on a post",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "post_id": {"type": "string", "description": "Post ID"},
                        "size": {"type": "integer", "default": 20},
                    },
                    "required": ["post_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "interaction_send_reply",
                "description": "Reply to a comment on a post. REQUIRES user approval before sending.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "post_id": {"type": "string", "description": "Post ID"},
                        "parent_comment_id": {"type": "string", "description": "The comment ID to reply to"},
                        "content": {"type": "string", "description": "Reply content"},
                    },
                    "required": ["post_id", "parent_comment_id", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analytics_get_post_performance",
                "description": "Get engagement metrics for a single post (likes, comments, views, shares, favorites)",
                "parameters": {
                    "type": "object",
                    "properties": {"post_id": {"type": "string", "description": "Post ID"}},
                    "required": ["post_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "analytics_get_account_summary",
                "description": "Get analytics summary for the current user's account (total posts, likes, comments, followers, etc.)",
                "parameters": {"type": "object", "properties": {}},
            },
        },
    ]


# ── Routes ───────────────────────────────────────────────────────

@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
) -> ConversationListResponse:
    """List conversations for the authenticated user, newest first."""
    auth = _get_auth(request)
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
    store = request.app.state.conversation_store
    store[conversation_id] = {
        **session.model_dump(mode="json"),
        "title": body.title,
        "created_at": now,
        "updated_at": now,
    }
    return _conversation_summary(store[conversation_id])


class RunAcceptedResponse(BaseModel):
    run_id: str
    conversation_id: str
    status: str
    events_url: str
    replayed: bool = False


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
    session = _get_session(request, conversation_id)
    # Enforce timezone from the request on this turn
    if body.timezone:
        session.timezone = body.timezone
    mcp = request.app.state.mcp
    llm = request.app.state.llm
    model = request.app.state.model

    trace_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    received_at = datetime.now(UTC)

    # Replay only public conversation messages.  Tool observations and model
    # reasoning are intentionally never sent to the frontend or stored here.
    msg_store = request.app.state.message_store
    conversation_history = [
        {"role": item["role"], "content": item["content"]}
        for item in msg_store.get(conversation_id, [])
        if item.get("role") in {"user", "assistant"}
    ]
    msg_store.setdefault(conversation_id, []).append({
        "role": "user", "content": body.content,
        "created_at": _now_iso(),
    })

    events: list[dict[str, Any]] = []
    tool_failure: dict[str, Any] | None = None
    successful_draft: dict[str, Any] | None = None
    successful_schedule: dict[str, Any] | None = None
    successful_revision: dict[str, Any] | None = None
    successful_schedule_update: dict[str, Any] | None = None

    async def emit_event(event_type: str, data: dict[str, Any]) -> None:
        events.append({"event": event_type, "data": data})

    async def tool_handler(
        tool_name: str, tool_args: dict[str, Any],
        _session: SessionContext, agent_run_id: str, tool_call_id: str,
    ) -> dict[str, Any]:
        # DeepSeek rejects dots in function names — convert _ to . for MCP lookup
        mcp_name = tool_name.replace("_", ".", 1) if "_" in tool_name else tool_name
        tool_args = _bind_target_tool_args(mcp_name, tool_args, _session)
        if mcp_name == "publication.schedule":
            # The model chooses the tool, but deterministic code owns the
            # user's relative time.
            tool_args = _normalize_schedule_tool_args(
                tool_args,
                user_message=body.content,
                timezone_name=_session.timezone or "Asia/Shanghai",
                now=received_at,
            )
        elif mcp_name == "publication.update_schedule":
            tool_args = _normalize_update_schedule_tool_args(
                tool_args,
                user_message=body.content,
                timezone_name=_session.timezone or "Asia/Shanghai",
                now=received_at,
            )
        if requires_approval(mcp_name):
            pending = _session.pending_approval
            if not pending or pending.operation != mcp_name:
                approval_id = str(uuid.uuid4())
                approval = PendingApproval(
                    approval_id=approval_id,
                    operation=mcp_name,
                    resource_id=(
                        str(tool_args["draft_id"])
                        if tool_args.get("draft_id") is not None
                        else None
                    ),
                    description=f"Approve {mcp_name}",
                )
                _session.pending_approval = approval
                _auth_store_put(
                    "approval_store",
                    request,
                    approval_id,
                    {
                        "approval_id": approval_id,
                        "conversation_id": conversation_id,
                        "run_id": agent_run_id,
                        "operation": mcp_name,
                        "resource_id": approval.resource_id,
                        "description": approval.description,
                        "status": "PENDING",
                    },
                )
                return {
                    "ok": False,
                    "code": "APPROVAL_REQUIRED",
                    "message": f"Tool '{mcp_name}' requires user approval",
                    "user_message": "此操作需要您的确认。请确认是否继续。",
                    "retryable": False,
                    "request_sent": False,
                    "trace_id": trace_id,
                }
        result = await mcp.execute_tool(
            mcp_name,
            auth=auth,
            session=_session,
            trace_id=trace_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            **tool_args,
        )
        return result

    async def on_tool_start(tool_name: str, tool_call_id: str, args: dict[str, Any]) -> None:
        await emit_event("TOOL_CALL_STARTED", {
            "run_id": run_id, "tool_name": tool_name, "tool_call_id": tool_call_id,
        })

    async def on_tool_complete(tool_name: str, tool_call_id: str, result: dict[str, Any]) -> None:
        nonlocal tool_failure, successful_draft, successful_schedule, successful_revision, successful_schedule_update
        event_type = "TOOL_CALL_COMPLETED" if result.get("ok") else "TOOL_CALL_FAILED"
        if not result.get("ok") and result.get("code") != "APPROVAL_REQUIRED":
            tool_failure = {
                "code": str(result.get("code") or "TOOL_EXECUTION_FAILED"),
                "tool_name": tool_name,
                "request_sent": bool(result.get("request_sent", False)),
                "retryable": bool(result.get("retryable", False)),
                "state": result.get("state"),
                "message": str(result.get("user_message") or "工具执行失败，请稍后重试。"),
            }
        if result.get("ok") and isinstance(result.get("data"), dict):
            data = result["data"]
            if tool_name == "content_create_draft":
                successful_draft = {
                    key: data.get(key)
                    for key in ("draft_id", "title")
                    if data.get(key) is not None
                }
            elif tool_name == "publication_schedule":
                successful_schedule = {
                    key: data.get(key)
                    for key in ("schedule_id", "draft_id", "run_at", "timezone", "status")
                    if data.get(key) is not None
                }
            elif tool_name == "content_revise_draft":
                successful_revision = {
                    key: data.get(key)
                    for key in (
                        "draft_id",
                        "title",
                        "version",
                        "updated_at",
                        "creator_task_id",
                        "creator_artifact_id",
                    )
                    if data.get(key) is not None
                }
            elif tool_name == "publication_update_schedule":
                successful_schedule_update = {
                    key: data.get(key)
                    for key in (
                        "schedule_id",
                        "draft_id",
                        "run_at",
                        "timezone",
                        "status",
                        "version",
                    )
                    if data.get(key) is not None
                }
        event_data: dict[str, Any] = {
            "run_id": run_id, "tool_name": tool_name, "tool_call_id": tool_call_id,
            "ok": result.get("ok"), "code": result.get("code"),
            "user_message": result.get("user_message"),
        }
        if result.get("ok") and isinstance(result.get("data"), dict):
            public_keys = (
                ("draft_id", "title", "creator_task_id", "creator_artifact_id")
                if tool_name == "content_create_draft"
                else (
                    "draft_id",
                    "title",
                    "version",
                    "updated_at",
                    "creator_task_id",
                    "creator_artifact_id",
                )
                if tool_name == "content_revise_draft"
                else ("draft_id", "schedule_id", "run_at", "timezone", "status")
                if tool_name in {"publication_schedule", "publication_update_schedule"}
                else ()
            )
            public_result = {
                key: result["data"].get(key)
                for key in public_keys
                if result["data"].get(key) is not None
            }
            if public_result:
                event_data["result"] = public_result
        await emit_event(event_type, event_data)

    async def on_assistant_delta(content: str) -> None:
        await emit_event("ASSISTANT_MESSAGE_DELTA", {"run_id": run_id, "content": content})

    await emit_event("RUN_STARTED", {
        "run_id": run_id, "conversation_id": conversation_id, "status": "IN_PROGRESS",
    })

    assistant = CommunityOperationsAssistant(
        llm=llm, model=model,
        tools_schema=_build_tool_schemas(),
        system_prompt=_SYSTEM_PROMPT,
    )

    try:
        result = await assistant.run(
            user_message=body.content,
            session=session,
            tool_handler=tool_handler,
            conversation_history=conversation_history,
            trace_id=trace_id,
            run_id=run_id,
            on_tool_start=on_tool_start,
            on_tool_complete=on_tool_complete,
            on_assistant_delta=on_assistant_delta,
        )
    except Exception:
        logger.exception("Run failed run_id=%s", run_id)
        _save_session(request, session)
        error_code = "MODEL_REQUEST_FAILED"
        error_message = "模型请求失败，请稍后重试。"
        await emit_event(
            "RUN_FAILED",
            {"run_id": run_id, "code": error_code, "message": error_message},
        )
        run_record = {
            "run_id": run_id, "conversation_id": conversation_id,
            "user_id": auth.user_id, "tenant_id": auth.tenant_id,
            "status": "FAILED", "error_code": error_code,
            "error": error_message,
            "trace_id": trace_id, "tool_rounds": 0, "events": events,
        }
        request.app.state.run_store[run_id] = run_record
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "code": error_code,
                "message": error_message,
                "run_id": run_id,
                "events_url": f"/api/v1/assistant/runs/{run_id}/events",
            },
        ) from None

    if tool_failure is not None:
        partial_failure = (
            successful_revision is not None
            and tool_failure.get("tool_name") == "publication_update_schedule"
        )
        failure_message = str(tool_failure["message"])
        if partial_failure:
            if tool_failure["code"] in {
                "INVALID_TOOL_ARGUMENT",
                "TOOL_ARGUMENT_VALIDATION_FAILED",
                "PRE_EXECUTION_VALIDATION_FAILED",
            }:
                failure_message = (
                    "\u8349\u7a3f\u5185\u5bb9\u5df2\u4fee\u6539\uff0c\u4f46\u53d1\u5e03\u65f6\u95f4\u53c2\u6570\u6821\u9a8c\u5931\u8d25\uff1b\u5b9a\u65f6\u4efb\u52a1\u5c1a\u672a\u4fee\u6539\uff0c\u53ef\u4ee5\u5b89\u5168\u91cd\u8bd5\u3002"
                )
            else:
                failure_message = (
                    "\u8349\u7a3f\u5185\u5bb9\u5df2\u4fee\u6539\uff0c\u4f46\u53d1\u5e03\u65f6\u95f4\u8c03\u6574\u5931\u8d25\uff0c\u8bf7\u68c0\u67e5\u5b9a\u65f6\u4efb\u52a1\u72b6\u6001\u3002"
                )
        raw_state = tool_failure.get("state")
        original_state: dict[str, Any] = (
            raw_state if isinstance(raw_state, dict) else {}
        )
        failure_state = {
            **original_state,
            "phase": "PARTIAL_FAILURE"
            if partial_failure
            else original_state.get("phase", "RUN_FAILED"),
            "draft_revision": "COMPLETED" if successful_revision else "NOT_STARTED",
            "schedule_update": "FAILED"
            if partial_failure
            else "NOT_STARTED",
            "downstream_called": bool(tool_failure.get("request_sent", False)),
            "side_effect_started": bool(tool_failure.get("request_sent", False)),
            "safe_to_retry": bool(tool_failure.get("retryable", False)),
        }
        _save_session(request, session)
        await emit_event(
            "RUN_PARTIAL_FAILURE" if partial_failure else "RUN_FAILED",
            {
                "run_id": run_id,
                "code": tool_failure["code"],
                "message": failure_message,
                "state": failure_state,
            },
        )
        request.app.state.run_store[run_id] = {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "user_id": auth.user_id,
            "tenant_id": auth.tenant_id,
            "status": "PARTIAL_FAILURE" if partial_failure else "FAILED",
            "error_code": tool_failure["code"],
            "error": failure_message,
            "trace_id": trace_id,
            "tool_rounds": result.get("tool_rounds", 0),
            "events": events,
            "partial_results": {
                "draft_revision": successful_revision,
                "schedule_update": successful_schedule_update,
            },
        }
        raise HTTPException(
            status_code=_http_status_for_tool_error(tool_failure["code"]),
            detail={
                "code": tool_failure["code"],
                "message": failure_message,
                "run_id": run_id,
                "events_url": f"/api/v1/assistant/runs/{run_id}/events",
                "state": failure_state,
            },
        )

    _save_session(request, session)

    # Save assistant response to message history.  The model's response is
    # supplemented with verified IDs/time so the frontend never has to infer
    # success from HTTP 202 alone.
    assistant_content = _append_schedule_confirmation(
        result.get("content", ""),
        draft=successful_revision or successful_draft,
        schedule=successful_schedule_update or successful_schedule,
    )
    result["content"] = assistant_content
    if assistant_content:
        msg_store.setdefault(conversation_id, []).append({
            "role": "assistant", "content": assistant_content,
            "trace_id": trace_id, "created_at": _now_iso(),
        })

    run_status = "WAITING_APPROVAL" if session.pending_approval else "COMPLETED"
    await emit_event(
        "RUN_WAITING_APPROVAL" if session.pending_approval else "RUN_COMPLETED",
        {
            "run_id": run_id,
            "content": result["content"],
            "tool_rounds": result["tool_rounds"],
        },
    )
    if run_status == "COMPLETED":
        session.last_successful_run_id = run_id
        _save_session(request, session)

    run_record = {
        "run_id": run_id, "conversation_id": conversation_id,
        "user_id": auth.user_id, "tenant_id": auth.tenant_id,
        "status": run_status, "content": result["content"],
        "trace_id": trace_id, "tool_rounds": result["tool_rounds"],
        "events": events, "session_snapshot": result.get("session_snapshot"),
        "approval_id": session.pending_approval.approval_id if session.pending_approval else None,
    }
    request.app.state.run_store[run_id] = run_record

    return RunAcceptedResponse(
        run_id=run_id, conversation_id=conversation_id,
        status=run_status,
        events_url=f"/api/v1/assistant/runs/{run_id}/events",
    )


@router.get("/conversations/{conversation_id}/messages")
async def get_messages(conversation_id: str, request: Request) -> list[MessageView]:
    """Get message history for a conversation (ownership-verified)."""
    auth = _get_auth(request)
    store = request.app.state.conversation_store
    if conversation_id not in store:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not _conversation_belongs_to(auth, store[conversation_id]):
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg_store = request.app.state.message_store
    return [
        MessageView(role=m["role"], content=m["content"],
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
    record = _auth_store_get("run_store", request, run_id)
    events = record.get("events", [])
    steps: list[dict[str, object]] = []
    for i, evt in enumerate(events):
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
    if approval_id:
        approval_record = request.app.state.approval_store.get(approval_id)
        if approval_record and _conversation_belongs_to(_get_auth(request), approval_record):
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
        approval=approval,
    )


@router.get("/runs/{run_id}/events")
async def get_run_events(run_id: str, request: Request) -> StreamingResponse:
    record = _auth_store_get("run_store", request, run_id)
    events = record.get("events", [])
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/runs")
async def list_runs(request: Request, limit: int = 30) -> list[RunResponse]:
    auth = _get_auth(request)
    run_store = request.app.state.run_store
    owned = [
        r for r in run_store.values()
        if r.get("user_id") == auth.user_id and r.get("tenant_id") == auth.tenant_id
    ]
    owned.sort(key=lambda r: r.get("trace_id", ""), reverse=True)
    return [
        RunResponse(
            run_id=r["run_id"], conversation_id=r["conversation_id"],
            goal="", status=r["status"],
            summary=(r.get("content") or "")[:200],
            final_response=r.get("content"),
            error_code=r.get("error_code"),
            error=r.get("error"),
            trace_id=r.get("trace_id"),
            budget={"model_calls": 1, "max_model_calls": 6, "tool_calls": r.get("tool_rounds", 0), "max_tool_calls": 30, "replan_count": 0, "max_replans": 0},
            timing={"queue_ms": None, "model_ms": 0, "tool_ms": 0, "dependency_wait_ms": 0, "total_ms": None},
            steps=[],
        )
        for r in owned[:limit]
    ]


@router.get("/runs/{run_id}/events/stream")
async def stream_run_events(run_id: str, request: Request) -> StreamingResponse:
    record = _auth_store_get("run_store", request, run_id)
    events = record.get("events", [])
    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request) -> RunResponse:
    record = _auth_store_get("run_store", request, run_id)
    record["status"] = "CANCELLED"
    record.setdefault("events", []).append({
        "event": "RUN_CANCELLED",
        "data": {"run_id": run_id},
    })
    return await get_run(run_id, request)


@router.post("/runs/{run_id}/interrupt")
async def interrupt_run(run_id: str, request: Request) -> RunResponse:
    record = _auth_store_get("run_store", request, run_id)
    record["status"] = "CANCELLED"
    record.setdefault("events", []).append({
        "event": "RUN_INTERRUPTED",
        "data": {"run_id": run_id},
    })
    return await get_run(run_id, request)


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


_SYSTEM_PROMPT = """你是 GreenBook 社区助手。

硬性规则：用户要求搜索社区、搜索帖子或查找社区主题时，必须先调用 community_search_public_posts，再根据工具真实返回回答；禁止直接用常识回答。用户要求写作并保存草稿时，直接调用 content_create_draft；除非用户同时明确要求参考社区帖子，否则不要先调用 community_search_public_posts。用户要求修改草稿时，必须调用 content_revise_draft；用户要求定时发布时，必须调用 publication_schedule。只有简单问候和一般知识问答不调用工具。

你可以帮助用户搜索社区、查询个人数据、创作和管理帖子、安排发布。社区默认指 GreenBook 站内社区；“我的帖子”只查询当前登录用户；“刚才那篇”优先指当前会话最近成功操作的草稿；相对时间使用用户时区。

不要编造工具未返回的事实，工具失败时如实说明；不要暴露 Token、密钥、reasoning_content 或内部堆栈；未明确提及外部平台时，不调用外部平台。"""
