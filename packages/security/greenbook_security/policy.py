"""Risk-based execution policy for tool operations.

Determines whether a tool requires approval before execution.
"""

from __future__ import annotations

from enum import Enum


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Read-only operations: auto-execute
LOW_RISK_TOOLS: set[str] = {
    "community.search_public_posts",
    "community.get_post",
    "community.list_own_posts",
    "content.get_draft",
    "content.list_drafts",
    "publication.get_status",
    "interaction.list_comments",
    "analytics.get_post_performance",
    "analytics.get_account_summary",
}

# Idempotent write operations: auto-execute with idempotency
MEDIUM_RISK_TOOLS: set[str] = {
    "content.create_draft",
    "content.revise_draft",
    "publication.schedule",
    "publication.update_schedule",
    "publication.cancel_schedule",
}

# Destructive or irreversible operations: require approval
HIGH_RISK_TOOLS: set[str] = {
    "publication.publish_now",
    "interaction.send_reply",
}


def tool_risk_level(tool_name: str) -> RiskLevel:
    if tool_name in LOW_RISK_TOOLS:
        return RiskLevel.LOW
    if tool_name in MEDIUM_RISK_TOOLS:
        return RiskLevel.MEDIUM
    if tool_name in HIGH_RISK_TOOLS:
        return RiskLevel.HIGH
    return RiskLevel.HIGH


def requires_approval(tool_name: str) -> bool:
    return tool_risk_level(tool_name) == RiskLevel.HIGH
