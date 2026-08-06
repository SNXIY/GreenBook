"""Assistant API routes — conversations, messages, runs, approvals, SSE events."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from greenbook_assistant_core.agent import CommunityOperationsAssistant
from greenbook_assistant_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
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


class MessageCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10_000)
    timezone: str = Field(default="Asia/Shanghai", max_length=64)


class RunResponse(BaseModel):
    run_id: str
    conversation_id: str
    status: str
    content: str | None = None
    trace_id: str | None = None
    tool_rounds: int = 0


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")


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

    # New conversation
    now = _now_iso()
    session = SessionContext(
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        timezone=auth.timezone,
    )
    store[conversation_id] = {
        **session.model_dump(mode="json"),
        "title": None,
        "created_at": now,
        "updated_at": now,
    }
    return session


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
                "name": "community.search_public_posts",
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
                "name": "community.get_post",
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
                "name": "community.list_own_posts",
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
                "name": "content.create_draft",
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
                "name": "content.get_draft",
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
                "name": "content.list_drafts",
                "description": "List the current user's drafts",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "content.revise_draft",
                "description": "Revise an existing draft. The system will regenerate content based on your revision instructions. If no draft_id specified, revises the most recent draft from this conversation.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "instruction": {"type": "string", "description": "Revision instruction: what to change, add, remove, adjust tone, etc."},
                        "draft_id": {"type": "string", "description": "Draft ID (optional)"},
                    },
                    "required": ["instruction"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication.schedule",
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
                "name": "publication.get_status",
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
                "name": "publication.update_schedule",
                "description": "Change the scheduled publication time. Only works for schedules in SCHEDULED status.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "schedule_id": {"type": "string", "description": "Schedule ID (optional)"},
                        "run_at": {"type": "string", "description": "New ISO-8601 datetime with timezone"},
                    },
                    "required": ["run_at"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "publication.cancel_schedule",
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
                "name": "publication.publish_now",
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
                "name": "interaction.list_comments",
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
                "name": "interaction.send_reply",
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
                "name": "analytics.get_post_performance",
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
                "name": "analytics.get_account_summary",
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


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    body: MessageCreateRequest,
    request: Request,
) -> StreamingResponse:
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

    events: list[dict[str, Any]] = []

    async def emit_event(event_type: str, data: dict[str, Any]) -> None:
        events.append({"event": event_type, "data": data})

    async def tool_handler(
        tool_name: str, tool_args: dict[str, Any],
        _session: SessionContext, agent_run_id: str, tool_call_id: str,
    ) -> dict[str, Any]:
        if requires_approval(tool_name):
            pending = _session.pending_approval
            if not pending or pending.operation != tool_name:
                return {
                    "ok": False,
                    "code": "APPROVAL_REQUIRED",
                    "message": f"Tool '{tool_name}' requires user approval",
                    "user_message": "此操作需要您的确认。请确认是否继续。",
                    "retryable": False,
                    "request_sent": False,
                    "trace_id": trace_id,
                }
        result = await mcp.execute_tool(
            tool_name,
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
        event_type = "TOOL_CALL_COMPLETED" if result.get("ok") else "TOOL_CALL_FAILED"
        await emit_event(event_type, {
            "run_id": run_id, "tool_name": tool_name, "tool_call_id": tool_call_id,
            "ok": result.get("ok"), "code": result.get("code"),
            "user_message": result.get("user_message"),
        })

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
            trace_id=trace_id,
            run_id=run_id,
            on_tool_start=on_tool_start,
            on_tool_complete=on_tool_complete,
            on_assistant_delta=on_assistant_delta,
        )
    except Exception as exc:
        logger.exception("Run failed run_id=%s", run_id)
        await emit_event("RUN_FAILED", {"run_id": run_id, "error": str(exc)})
        run_record = {
            "run_id": run_id, "conversation_id": conversation_id,
            "user_id": auth.user_id, "tenant_id": auth.tenant_id,
            "status": "FAILED", "error": str(exc),
            "trace_id": trace_id, "tool_rounds": 0, "events": events,
        }
        request.app.state.run_store[run_id] = run_record
        return StreamingResponse(
            _sse_stream(run_record["events"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    _save_session(request, session)

    await emit_event("RUN_COMPLETED", {
        "run_id": run_id, "content": result["content"],
        "tool_rounds": result["tool_rounds"],
    })

    run_record = {
        "run_id": run_id, "conversation_id": conversation_id,
        "user_id": auth.user_id, "tenant_id": auth.tenant_id,
        "status": "COMPLETED", "content": result["content"],
        "trace_id": trace_id, "tool_rounds": result["tool_rounds"],
        "events": events, "session_snapshot": result.get("session_snapshot"),
    }
    request.app.state.run_store[run_id] = run_record

    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> RunResponse:
    record = _auth_store_get("run_store", request, run_id)
    return RunResponse(
        run_id=record["run_id"],
        conversation_id=record["conversation_id"],
        status=record["status"],
        content=record.get("content"),
        trace_id=record.get("trace_id"),
        tool_rounds=record.get("tool_rounds", 0),
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


_SYSTEM_PROMPT = """你是 GreenBook 社区的运营助手，也是知光社区的官方创作伙伴。

## 你的能力
- 日常问候和GreenBook产品帮助
- 普通知识问答
- 搜索和浏览社区公共帖子
- 查询用户自己的帖子和草稿
- 通过AI创作社区帖子内容
- 管理草稿（创建、修改、查看）
- 定时发布管理（创建、修改、取消）
- 查看评论和回复
- 帖子数据分析和账号运营分析
- 基于真实数据生成运营建议

## 产品默认语义
- "社区"默认指GreenBook站内公共社区
- "热门帖子"默认调用公共搜索
- "我的帖子"只查询当前登录用户
- "发布"默认发布到GreenBook当前账号
- "刚才那篇"优先指当前对话最近创建的草稿
- 相对时间使用用户时区
- 未明确要求全网搜索时，不调用外部搜索
- 未明确提及外部平台时，不询问发布平台
- 搜索结果是创作输入，不是搜索完成后直接结束

## 重要规则
- 不要编造未发生的事实
- 不要声称已完成的动作（如果工具调用失败就如实告知）
- 不要暴露系统内部信息（密钥、Token、堆栈）
- 只在你确实需要真实数据时才调用工具
- 简单问候和知识问答直接回复，不需要调用任何工具"""
