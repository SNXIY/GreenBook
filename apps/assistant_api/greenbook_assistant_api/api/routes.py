"""Assistant API routes — conversations, messages, runs, approvals, SSE events."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from greenbook_assistant_core.agent import CommunityOperationsAssistant
from greenbook_assistant_core.context import SessionContext, PendingApproval
from greenbook_contracts.identity import AuthContext
from greenbook_security.approval import Approval
from greenbook_security.policy import requires_approval
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assistant")


# ── Request / Response models ────────────────────────────────────

class ConversationCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=120)
    context_post_id: str | None = Field(default=None, max_length=64)


class ConversationResponse(BaseModel):
    conversation_id: str
    title: str | None = None
    created_at: str


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


def _get_session(request: Request, conversation_id: str) -> SessionContext:
    store = request.app.state.conversation_store
    if conversation_id in store:
        data = store[conversation_id]
        return SessionContext(**data)
    auth = _get_auth(request)
    session = SessionContext(
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        timezone=auth.timezone,
    )
    store[conversation_id] = session.model_dump(mode="json")
    return session


def _save_session(request: Request, session: SessionContext) -> None:
    store = request.app.state.conversation_store
    store[session.conversation_id] = session.model_dump(mode="json")


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

@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
) -> ConversationResponse:
    conversation_id = str(uuid.uuid4())
    auth = _get_auth(request)
    session = SessionContext(
        conversation_id=conversation_id,
        user_id=auth.user_id,
        tenant_id=auth.tenant_id,
        timezone=auth.timezone,
    )
    _save_session(request, session)
    return ConversationResponse(
        conversation_id=conversation_id,
        title=body.title,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    body: MessageCreateRequest,
    request: Request,
) -> StreamingResponse:
    auth = _get_auth(request)
    session = _get_session(request, conversation_id)
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
        session_ctx: SessionContext, agent_run_id: str, tool_call_id: str,
    ) -> dict[str, Any]:
        # Check if approval is needed
        if requires_approval(tool_name):
            pending = session_ctx.pending_approval
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
            session=session_ctx,
            trace_id=trace_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            **tool_args,
        )
        return result

    async def on_tool_start(tool_name: str, tool_call_id: str, args: dict[str, Any]) -> None:
        await emit_event("TOOL_CALL_STARTED", {
            "run_id": run_id,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
        })

    async def on_tool_complete(tool_name: str, tool_call_id: str, result: dict[str, Any]) -> None:
        event_type = "TOOL_CALL_COMPLETED" if result.get("ok") else "TOOL_CALL_FAILED"
        await emit_event(event_type, {
            "run_id": run_id,
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "ok": result.get("ok"),
            "code": result.get("code"),
            "user_message": result.get("user_message"),
        })

    async def on_assistant_delta(content: str) -> None:
        await emit_event("ASSISTANT_MESSAGE_DELTA", {
            "run_id": run_id,
            "content": content,
        })

    await emit_event("RUN_STARTED", {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "status": "IN_PROGRESS",
    })

    assistant = CommunityOperationsAssistant(
        llm=llm,
        model=model,
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
        await emit_event("RUN_FAILED", {
            "run_id": run_id,
            "error": str(exc),
        })
        run_record = {
            "run_id": run_id,
            "conversation_id": conversation_id,
            "status": "FAILED",
            "error": str(exc),
            "trace_id": trace_id,
            "tool_rounds": 0,
            "events": events,
        }
        request.app.state.run_store[run_id] = run_record
        return StreamingResponse(
            _sse_stream(run_record["events"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    _save_session(request, session)

    await emit_event("RUN_COMPLETED", {
        "run_id": run_id,
        "content": result["content"],
        "tool_rounds": result["tool_rounds"],
    })

    run_record = {
        "run_id": run_id,
        "conversation_id": conversation_id,
        "status": "COMPLETED",
        "content": result["content"],
        "trace_id": trace_id,
        "tool_rounds": result["tool_rounds"],
        "events": events,
        "session_snapshot": result.get("session_snapshot"),
    }
    request.app.state.run_store[run_id] = run_record

    return StreamingResponse(
        _sse_stream(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> RunResponse:
    run_store = request.app.state.run_store
    if run_id not in run_store:
        raise HTTPException(status_code=404, detail="Run not found")
    record = run_store[run_id]
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
    run_store = request.app.state.run_store
    if run_id not in run_store:
        raise HTTPException(status_code=404, detail="Run not found")
    events = run_store[run_id].get("events", [])
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
    approval_store = request.app.state.approval_store
    if approval_id not in approval_store:
        raise HTTPException(status_code=404, detail="Approval not found")

    if body.decision != "APPROVE":
        approval_store[approval_id]["status"] = "REJECTED"
        return {"approval_id": approval_id, "status": "REJECTED"}

    approval_store[approval_id]["status"] = "APPROVED"

    approval_data = approval_store[approval_id]
    conv_id = approval_data.get("conversation_id", "")
    if conv_id in request.app.state.conversation_store:
        session_data = request.app.state.conversation_store[conv_id]
        session = SessionContext(**session_data)
        session.pending_approval = None  # Clear after approval
        request.app.state.conversation_store[conv_id] = session.model_dump(mode="json")

    return {"approval_id": approval_id, "status": "APPROVED"}


@router.post("/approvals/{approval_id}/reject")
async def reject_operation(
    approval_id: str,
    request: Request,
) -> dict[str, Any]:
    approval_store = request.app.state.approval_store
    if approval_id not in approval_store:
        raise HTTPException(status_code=404, detail="Approval not found")
    approval_store[approval_id]["status"] = "REJECTED"
    return {"approval_id": approval_id, "status": "REJECTED"}


# ── Helpers ──────────────────────────────────────────────────────

async def _sse_stream(events: list[dict[str, Any]]):
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
