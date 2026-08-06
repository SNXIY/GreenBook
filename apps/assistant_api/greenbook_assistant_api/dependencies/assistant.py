from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from greenbook_assistant_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_java_client import JavaClient

logger = logging.getLogger(__name__)


class AssistantDeps:
    """Injectable dependencies for the assistant API."""

    def __init__(
        self,
        java: JavaClient,
        auth: AuthContext,
        conversation_id: str,
        trace_id: str,
    ) -> None:
        self.java = java
        self.auth = auth
        self.conversation_id = conversation_id
        self.trace_id = trace_id


def create_tool_handler(
    java: JavaClient,
    auth: AuthContext,
    conversation_id: str,
    trace_id: str,
) -> Callable[[str, dict[str, Any], SessionContext], Any]:
    """Create a tool handler that routes tool calls to Java backend."""

    async def handle_tool(
        tool_name: str, args: dict[str, Any], session: SessionContext
    ) -> dict[str, Any]:
        bearer = getattr(auth, "token", None)

        if tool_name.startswith("community_"):
            return await _handle_community(java, tool_name, args, bearer, conversation_id, trace_id)
        elif tool_name.startswith("content_"):
            return await _handle_content(java, tool_name, args, bearer, conversation_id, trace_id)
        elif tool_name.startswith("publication_"):
            return await _handle_publication(java, tool_name, args, bearer, conversation_id, trace_id)
        elif tool_name.startswith("interaction_"):
            return await _handle_interaction(java, tool_name, args, bearer, conversation_id, trace_id)
        elif tool_name.startswith("analytics_"):
            return await _handle_analytics(java, tool_name, args, bearer, conversation_id, trace_id)
        else:
            return {
                "ok": False,
                "code": "NOT_FOUND",
                "message": f"Unknown tool: {tool_name}",
                "user_message": "This operation is not available.",
            }

    return handle_tool


async def _handle_community(
    java: JavaClient, tool: str, args: dict, bearer: str | None,
    cid: str, tid: str,
) -> dict[str, Any]:
    if tool == "community_search_public_posts":
        r = await java.post("/api/v1/posts/search", body=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "community_get_post":
        r = await java.get(f"/api/v1/posts/{args['post_id']}", bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "community_list_own_posts":
        r = await java.get("/api/v1/posts/mine", params=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    else:
        return {"ok": False, "code": "NOT_FOUND", "message": f"Unknown community tool: {tool}"}
    return r.model_dump()


async def _handle_content(
    java: JavaClient, tool: str, args: dict, bearer: str | None,
    cid: str, tid: str,
) -> dict[str, Any]:
    if tool == "content_create_draft":
        r = await java.post("/api/v1/drafts", body=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
        if r.ok and r.data and r.data.get("draft_id"):
            v = await java.get(f"/api/v1/drafts/{r.data['draft_id']}", bearer_token=bearer, conversation_id=cid, run_id=tid)
            if v.ok:
                r.data = v.data
    elif tool == "content_get_draft":
        r = await java.get(f"/api/v1/drafts/{args['draft_id']}", bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "content_revise_draft":
        r = await java.put(f"/api/v1/drafts/{args['draft_id']}", body=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "content_list_drafts":
        r = await java.get("/api/v1/drafts/mine", params=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    else:
        return {"ok": False, "code": "NOT_FOUND", "message": f"Unknown content tool: {tool}"}
    return r.model_dump()


async def _handle_publication(
    java: JavaClient, tool: str, args: dict, bearer: str | None,
    cid: str, tid: str,
) -> dict[str, Any]:
    if tool == "publication_schedule":
        r = await java.post("/api/v1/publication/schedules", body=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
        if r.ok and r.data and r.data.get("schedule_id"):
            v = await java.get(f"/api/v1/publication/schedules/{r.data['schedule_id']}", bearer_token=bearer, conversation_id=cid, run_id=tid)
            if v.ok:
                r.data = v.data
    elif tool == "publication_update_schedule":
        r = await java.put(f"/api/v1/publication/schedules/{args['schedule_id']}", body=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "publication_cancel_schedule":
        r = await java.delete(f"/api/v1/publication/schedules/{args['schedule_id']}", bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "publication_get_status":
        r = await java.get(f"/api/v1/publication/schedules/{args['schedule_id']}", bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "publication_publish_now":
        return {"ok": True, "code": "APPROVAL_REQUIRED", "message": "publish_now requires user confirmation", "data": {"draft_id": args["draft_id"], "requires_approval": True}}
    else:
        return {"ok": False, "code": "NOT_FOUND", "message": f"Unknown publication tool: {tool}"}
    return r.model_dump()


async def _handle_interaction(
    java: JavaClient, tool: str, args: dict, bearer: str | None,
    cid: str, tid: str,
) -> dict[str, Any]:
    if tool == "interaction_list_comments":
        r = await java.get(f"/api/v1/posts/{args['post_id']}/comments", params=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "interaction_reply_to_comment":
        r = await java.post(f"/api/v1/posts/{args['post_id']}/comments", body=args, bearer_token=bearer, conversation_id=cid, run_id=tid)
    else:
        return {"ok": False, "code": "NOT_FOUND", "message": f"Unknown interaction tool: {tool}"}
    return r.model_dump()


async def _handle_analytics(
    java: JavaClient, tool: str, args: dict, bearer: str | None,
    cid: str, tid: str,
) -> dict[str, Any]:
    if tool == "analytics_get_post_performance":
        r = await java.get(f"/api/v1/analytics/posts/{args['post_id']}/performance", bearer_token=bearer, conversation_id=cid, run_id=tid)
    elif tool == "analytics_suggest_topics":
        r = await java.get("/api/v1/analytics/topics/suggest", bearer_token=bearer, conversation_id=cid, run_id=tid)
    else:
        return {"ok": False, "code": "NOT_FOUND", "message": f"Unknown analytics tool: {tool}"}
    return r.model_dump()
