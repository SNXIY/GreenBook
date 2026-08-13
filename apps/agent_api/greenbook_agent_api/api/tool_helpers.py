"""Pure tool-call helpers shared by the Agent API and Runtime services.

Extracted from routes.py to avoid circular imports between the HTTP
layer and the service layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.time_parser import (
    format_local_schedule_time,
    parse_natural_schedule_time,
)
from greenbook_mcp_server.tool_schemas import (
    ReviseDraftArguments,
    UpdateScheduleArguments,
    openai_parameters,
)


def normalize_schedule_tool_args(
    tool_args: dict[str, Any],
    *,
    user_message: str,
    timezone_name: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Normalize a model schedule call to the Agent→Java contract."""
    normalized = dict(tool_args)
    normalized.pop("timezone_name", None)
    parsed_run_at = parse_natural_schedule_time(
        user_message, timezone_name, now=now,
    )
    if parsed_run_at:
        normalized["run_at"] = parsed_run_at
    normalized["timezone"] = timezone_name
    return normalized


def normalize_update_schedule_tool_args(
    tool_args: dict[str, Any],
    *,
    user_message: str,
    timezone_name: str,
    now: datetime,
) -> dict[str, Any]:
    """Own relative update times deterministically at message receipt time."""
    normalized = dict(tool_args)
    if "publish_at" in normalized and "run_at" not in normalized:
        normalized["run_at"] = normalized.pop("publish_at")
    parsed_run_at = parse_natural_schedule_time(
        user_message, timezone_name, now=now,
    )
    if parsed_run_at:
        normalized["run_at"] = parsed_run_at
    return normalized


def community_reference_items(data: Any) -> list[dict[str, Any]]:
    """Extract trusted public-search items for the next draft creation step."""
    if hasattr(data, "model_dump"):
        data = data.model_dump(mode="json")
    if isinstance(data, dict):
        raw_items = data.get("items") or data.get("posts") or []
    elif isinstance(data, list):
        raw_items = data
    else:
        return []

    references: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if hasattr(raw_item, "model_dump"):
            raw_item = raw_item.model_dump(mode="json")
        if not isinstance(raw_item, dict):
            continue
        post_id = raw_item.get("post_id") or raw_item.get("postId") or raw_item.get("id")
        if not post_id:
            continue
        references.append(dict(raw_item))
        if len(references) >= 8:
            break
    return references


def bind_target_tool_args(
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


def append_schedule_confirmation(
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


def build_tool_schemas() -> list[dict[str, Any]]:
    """Build OpenAI function-calling JSON schemas for all registered tools."""
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
