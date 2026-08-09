from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import ToolResult

logger = logging.getLogger(__name__)


async def revise_draft_via_creator(
    *,
    java: Any,
    creator: Any,
    draft_id: str,
    instruction: str,
    user_id: str,
    bearer_token: str | None = None,
    trace_id: str | None = None,
) -> ToolResult[dict[str, Any]]:
    """Orchestrate: read Draft from Java → Creator revise → update Java → verify schedule.

    1. Read current draft from Java
    2. Submit revision to Creator Agent
    3. Update the draft in Java (or create new version)
    4. Verify schedule still points to correct draft
    """

    # Step 1: Read current draft
    current = await java.get(
        f"/api/v1/drafts/{draft_id}",
        bearer_token=bearer_token,
    )
    if not current.ok:
        return current

    current_draft = current.data or {}

    # Step 2: Submit to Creator for revision
    creator_result = await creator.submit_task(
        "revise_draft",
        {
            "draft_id": draft_id,
            "current_content": current_draft.get("content", ""),
            "instruction": instruction,
        },
        trace_id=trace_id,
    )
    if not creator_result.ok:
        return ToolResult.dependency_unavailable(
            f"Creator revision failed: {creator_result.message}",
            trace_id=trace_id,
        )

    revised = creator_result.data or {}

    # Step 3: Update Java
    java_result = await java.put(
        f"/api/v1/drafts/{draft_id}",
        body={
            "title": revised.get("title", current_draft.get("title", "")),
            "content": revised.get("content", ""),
            "user_id": user_id,
        },
        bearer_token=bearer_token,
    )
    if not java_result.ok:
        return java_result

    # Step 4: Verify schedule consistency
    verify = await java.get(
        f"/api/v1/drafts/{draft_id}",
        bearer_token=bearer_token,
    )
    if verify.ok:
        java_result.data["verified"] = True
        java_result.data["version"] = verify.data.get("version", 1)
        java_result.data["schedule_id"] = verify.data.get("schedule_id")

    return java_result
