"""MCP Tool Registry — maps tool names to handler functions with Pydantic schemas."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from .context import ToolContext
from .tools import analytics, community, content, interaction, publication


class ToolDefinition(BaseModel):
    name: str
    description: str
    handler: Callable
    category: str
    risk: str  # low, medium, high


_TOOLS: dict[str, ToolDefinition] = {}


def _register(
    name: str,
    handler: Callable,
    *,
    description: str,
    category: str,
    risk: str,
) -> None:
    _TOOLS[name] = ToolDefinition(
        name=name,
        description=description,
        handler=handler,
        category=category,
        risk=risk,
    )


# ── Community ────────────────────────────────────────────────────

_register(
    "community.search_public_posts",
    community.search_public_posts,
    description="Search public posts in the GreenBook community",
    category="community",
    risk="low",
)
_register(
    "community.get_post",
    community.get_post,
    description="Get a single public post by ID",
    category="community",
    risk="low",
)
_register(
    "community.list_own_posts",
    community.list_own_posts,
    description="List the current user's own posts",
    category="community",
    risk="low",
)

# ── Content ──────────────────────────────────────────────────────

_register(
    "content.create_draft",
    content.create_draft,
    description="Create a new draft via Creator Agent and Java Facade",
    category="content",
    risk="medium",
)
_register(
    "content.get_draft",
    content.get_draft,
    description="Get a draft by ID",
    category="content",
    risk="low",
)
_register(
    "content.list_drafts",
    content.list_drafts,
    description="List the current user's drafts",
    category="content",
    risk="low",
)
_register(
    "content.revise_draft",
    content.revise_draft,
    description="Revise an existing draft via Creator Agent",
    category="content",
    risk="medium",
)

# ── Publication ──────────────────────────────────────────────────

_register(
    "publication.schedule",
    publication.schedule,
    description="Schedule a draft for publication",
    category="publication",
    risk="medium",
)
_register(
    "publication.get_status",
    publication.get_status,
    description="Get the current status of a scheduled publication",
    category="publication",
    risk="low",
)
_register(
    "publication.update_schedule",
    publication.update_schedule,
    description="Update a scheduled publication's run_at time",
    category="publication",
    risk="medium",
)
_register(
    "publication.cancel_schedule",
    publication.cancel_schedule,
    description="Cancel a scheduled publication",
    category="publication",
    risk="medium",
)
_register(
    "publication.publish_now",
    publication.publish_now,
    description="Immediately publish a draft (requires approval)",
    category="publication",
    risk="high",
)

# ── Interaction ──────────────────────────────────────────────────

_register(
    "interaction.list_comments",
    interaction.list_comments,
    description="List comments on a post",
    category="interaction",
    risk="low",
)
_register(
    "interaction.send_reply",
    interaction.send_reply,
    description="Reply to a comment (requires approval)",
    category="interaction",
    risk="high",
)

# ── Analytics ────────────────────────────────────────────────────

_register(
    "analytics.get_post_performance",
    analytics.get_post_performance,
    description="Get performance metrics for a single post",
    category="analytics",
    risk="low",
)
_register(
    "analytics.get_account_summary",
    analytics.get_account_summary,
    description="Get analytics summary for the current account",
    category="analytics",
    risk="low",
)


def get_tool(name: str) -> ToolDefinition:
    if name not in _TOOLS:
        raise ValueError(f"Unknown tool: {name}")
    return _TOOLS[name]


def list_tools() -> list[ToolDefinition]:
    return list(_TOOLS.values())


def tool_catalog_prompt() -> str:
    """Build a compact tool catalog for LLM function-calling context."""
    lines: list[str] = []
    for tool in _TOOLS.values():
        lines.append(f"- {tool.name}: {tool.description} (risk: {tool.risk})")
    return "\n".join(lines)
