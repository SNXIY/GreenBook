from __future__ import annotations

import logging
from typing import Any

from greenbook_contracts.tool_result import ToolResult
from greenbook_creator_client import CreatorClient
from greenbook_java_client import JavaClient

logger = logging.getLogger(__name__)


async def create_draft_via_creator(
    *,
    java: JavaClient,
    creator: CreatorClient,
    title: str,
    instruction: str,
    user_id: str,
    bearer_token: str | None = None,
    trace_id: str | None = None,
) -> ToolResult[dict[str, Any]]:
    """Orchestrate: submit to Creator → get FinalContent → call Java → verify.

    This is the correct multi-step flow:
    1. Submit a content creation task to Creator Agent
    2. Creator researches, outlines, writes, critiques, revises, finalizes
    3. Receive finalized content
    4. Call Java to persist the draft
    5. Verify the draft was created
    """

    # Step 1-2: Submit to Creator
    creator_result = await creator.submit_task(
        "create_draft",
        {"title": title, "instruction": instruction},
        trace_id=trace_id,
    )
    if not creator_result.ok:
        return ToolResult.dependency_unavailable(
            f"Creator Agent failed: {creator_result.message}",
            trace_id=trace_id,
        )

    final_content = creator_result.data or {}
    draft_title = final_content.get("title", title)
    draft_body = final_content.get("content", "")

    # Step 4: Persist via Java
    java_result = await java.post(
        "/api/v1/drafts",
        body={
            "title": draft_title,
            "content": draft_body,
            "user_id": user_id,
        },
        bearer_token=bearer_token,
        run_id=trace_id,
    )
    if not java_result.ok:
        return java_result

    # Step 5: Verify
    draft_id = java_result.data.get("draft_id", "")
    verify = await java.get(
        f"/api/v1/drafts/{draft_id}",
        bearer_token=bearer_token,
    )
    if verify.ok:
        java_result.data["verified"] = True
        java_result.data["version"] = verify.data.get("version", 1)

    return java_result
