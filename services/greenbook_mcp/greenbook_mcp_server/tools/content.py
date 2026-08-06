"""Content tools — create and revise drafts via Creator Agent + Java Facade.

content.create_draft:
  1. Receive topic + optional reference posts
  2. Create Creator Task
  3. Wait for Creator to complete
  4. Get FinalContent
  5. Call Java POST /drafts with Idempotency-Key
  6. Verify via GET /drafts/{draftId}
  7. Update SessionContext.active_draft_id

content.revise_draft:
  1. Resolve draftId from SessionContext
  2. Java GET /drafts/{draftId}
  3. Get DraftResponse.updatedAt
  4. Submit current content to Creator revise
  5. Java PUT /drafts/{draftId} with expectedVersion=updatedAt
  6. Handle HTTP 409 DRAFT_VERSION_CONFLICT
  7. Verify via GET again
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from greenbook_contracts.tool_result import ResourceRef, ToolResult
from greenbook_creator_client.client import CreatorClient, extract_creator_document
from greenbook_java_client.models import (
    AgentDraftCreateRequest,
    AgentDraftUpdateRequest,
    DraftResponse,
)

from ..context import ToolContext

logger = logging.getLogger(__name__)


async def create_draft(
    ctx: ToolContext,
    title: str,
    instruction: str,
    references: list[dict[str, Any]] | None = None,
    summary: str | None = None,
) -> ToolResult[dict[str, Any]]:
    """Create a new draft via Creator Agent → Java Facade."""
    trace_id = ctx.trace_id
    refs = references or []

    # Build reference notes from search results
    reference_notes = ""
    if refs:
        compact = [
            {
                "id": str(r.get("post_id") or r.get("id", ""))[:256],
                "title": str(r.get("title", ""))[:200],
                "summary": str(r.get("description") or r.get("summary", ""))[:600],
                "body_excerpt": str(r.get("body") or r.get("body_markdown", ""))[:1600],
            }
            for r in refs[:8]
            if r.get("post_id") or r.get("id")
        ]
        reference_notes = "\n\n".join(
            f"[{item['id']}] {item['title']}\n{item['summary']}\n{item['body_excerpt']}"
            for item in compact
        )[:12_000]

    # Step 1: Submit Creator task with AUTO interaction mode
    idempotency_key = ctx.idempotency_key("create_draft")

    creator_result = await ctx.creator.create_task(
        kind="CREATE_CONTENT",
        goal=instruction,
        constraints={
            "interaction_mode": "AUTO",
            "format": "POST",
            "target_length": 1200,
            "tone": "PRACTICAL",
            "audience": "知光知识社区用户",
        },
        reference_notes=reference_notes,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    if not creator_result.ok:
        return creator_result

    task_id = str(creator_result.data.get("task_id", ""))
    if not task_id:
        return ToolResult.internal_error("Creator returned a task without task_id", trace_id=trace_id)

    # Step 2: Wait for Creator completion
    wait_result = await ctx.creator.wait_for_completion(
        task_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
        deadline_seconds=240.0,
    )
    if not wait_result.ok:
        return wait_result

    snapshot = wait_result.data or {}
    final_artifact_id = str(snapshot.get("final_artifact_id") or "")

    # Step 3: Get the finalized artifact
    document: dict[str, str | None] = {"title": title, "description": summary, "body_markdown": instruction}
    creator_artifact_id: str | None = None

    if final_artifact_id:
        artifact_result = await ctx.creator.get_artifact(
            task_id, final_artifact_id,
            bearer_token=ctx.auth.raw_access_token,
            trace_id=trace_id,
        )
        if artifact_result.ok:
            doc = extract_creator_document(artifact_result.data or {})
            document = {
                "title": doc["title"] or title,
                "description": doc["description"] or summary,
                "body_markdown": doc["body_markdown"] or instruction,
            }
            creator_artifact_id = final_artifact_id

    # Step 4: Create handoff and get content
    handoff_result = await ctx.creator.create_handoff(
        task_id,
        source_artifact_id=creator_artifact_id,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=f"{idempotency_key}-handoff",
        trace_id=trace_id,
    )

    # Step 5: Call Java POST /drafts
    java_create = AgentDraftCreateRequest(
        title=str(document["title"] or title)[:256],
        content=str(document["body_markdown"] or instruction),
        summary=str(document["description"] or "")[:200] if document.get("description") else None,
    )

    draft_result = await ctx.java.create_draft(
        java_create,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not draft_result.ok:
        return draft_result

    draft = draft_result.data
    if not isinstance(draft, DraftResponse):
        return ToolResult.internal_error("Unexpected draft response type", trace_id=trace_id)

    draft_id = draft.draft_id

    # Step 6: Verify via GET /drafts/{draftId}
    verify_result = await ctx.java.get_draft(
        draft_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verify_result.ok:
        return ToolResult.internal_error(
            f"Draft {draft_id} created but GET verification failed: {verify_result.message}",
            trace_id=trace_id,
        )

    # Step 7: Update SessionContext
    ctx.session.active_draft_id = draft_id
    ctx.session.record_entity(
        ref=f"draft:{draft_id}", kind="DRAFT", entity_id=draft_id,
        label=draft.title, status="READY", run_id=ctx.agent_run_id,
    )

    resource_refs = [
        ResourceRef(ref=f"draft:{draft_id}", kind="DRAFT", resource_id=draft_id),
        ResourceRef(ref=f"task:{task_id}", kind="CREATOR_TASK", resource_id=task_id),
    ]
    if creator_artifact_id:
        resource_refs.append(
            ResourceRef(ref=f"artifact:{creator_artifact_id}", kind="CREATOR_ARTIFACT", resource_id=creator_artifact_id)
        )

    return ToolResult.success(
        {
            "draft_id": draft_id,
            "title": draft.title,
            "content": draft.content,
            "summary": draft.summary,
            "status": draft.status,
            "version": draft.version,
            "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
            "creator_task_id": task_id,
            "creator_artifact_id": creator_artifact_id,
        },
        trace_id=trace_id,
        receipt_id=draft_result.receipt_id,
        resource_refs=resource_refs,
    )


async def get_draft(
    ctx: ToolContext,
    draft_id: str | None = None,
) -> ToolResult[dict[str, Any]]:
    """Get a draft by ID. If no draft_id provided, resolves from session context."""
    resolved_id = draft_id or ctx.session.active_draft_id
    if not resolved_id:
        candidates: list[str] = []
        resolved_id, candidates = ctx.session.resolve_active_draft_id()
        if not resolved_id and candidates:
            return ToolResult.validation_error(
                "Multiple drafts found in current session. Please specify which one.",
                user_message="当前会话有多个草稿，请问您指的是哪一个？",
            )
        if not resolved_id:
            return ToolResult.not_found("No active draft in current session")

    result = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
        )
    return result


async def list_drafts(
    ctx: ToolContext,
) -> ToolResult[dict[str, Any]]:
    """List current user's drafts."""
    result = await ctx.java.list_own_drafts(
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        items = [d.model_dump(mode="json") for d in result.data]
        return ToolResult.success(items, trace_id=result.trace_id)
    return result


async def revise_draft(
    ctx: ToolContext,
    instruction: str,
    draft_id: str | None = None,
) -> ToolResult[dict[str, Any]]:
    """Revise an existing draft via Creator Agent → Java Facade.

    Uses expectedVersion=updatedAt from Java for optimistic concurrency control.
    """
    trace_id = ctx.trace_id

    # Step 1: Resolve draft_id
    resolved_id = draft_id or ctx.session.active_draft_id
    if not resolved_id:
        resolved_id, candidates = ctx.session.resolve_active_draft_id()
        if not resolved_id and candidates:
            return ToolResult.validation_error(
                "Multiple drafts found. Please specify which one to revise.",
                user_message="当前会话有多个草稿，请问您要修改哪一个？",
            )
        if not resolved_id:
            return ToolResult.not_found("No draft to revise. Create one first.")

    # Step 2: GET current draft from Java
    current = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not current.ok:
        return current

    draft_data = current.data
    if not isinstance(draft_data, DraftResponse):
        return ToolResult.internal_error("Unexpected draft response type", trace_id=trace_id)

    current_updated_at = draft_data.updated_at
    current_content = draft_data.content or ""
    current_title = draft_data.title or ""

    # Step 3: Submit Creator revise task
    idempotency_key = ctx.idempotency_key("revise_draft")

    creator_result = await ctx.creator.create_task(
        kind="IMPROVE_DRAFT",
        goal=f"Revise the following draft: {instruction}",
        constraints={
            "interaction_mode": "AUTO",
            "format": "POST",
            "target_length": 1200,
            "tone": "PRACTICAL",
            "draft": {
                "title": current_title,
                "body_markdown": current_content,
            },
        },
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    if not creator_result.ok:
        return creator_result

    task_id = str(creator_result.data.get("task_id", ""))
    if not task_id:
        return ToolResult.internal_error("Creator returned a task without task_id", trace_id=trace_id)

    # Step 4: Wait for Creator completion
    wait_result = await ctx.creator.wait_for_completion(
        task_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
    )
    if not wait_result.ok:
        return wait_result

    snapshot = wait_result.data or {}
    final_artifact_id = str(snapshot.get("final_artifact_id") or "")

    # Step 5: Get revised content
    revised_content = instruction  # fallback
    revised_title = current_title
    revised_summary = None

    if final_artifact_id:
        artifact_result = await ctx.creator.get_artifact(
            task_id, final_artifact_id,
            bearer_token=ctx.auth.raw_access_token,
            trace_id=trace_id,
        )
        if artifact_result.ok:
            doc = extract_creator_document(artifact_result.data or {})
            revised_title = doc["title"] or current_title
            revised_content = doc["body_markdown"] or instruction
            revised_summary = doc["description"]

    # Step 6: Java PUT with expectedVersion=updatedAt (ISO-8601)
    expected_version_str = current_updated_at.isoformat() if current_updated_at else None

    update_request = AgentDraftUpdateRequest(
        title=revised_title,
        content=revised_content,
        summary=revised_summary,
        expectedVersion=expected_version_str,  # noqa: F841
    )
    update_dict = {
        "title": revised_title,
        "content": revised_content,
        "summary": revised_summary,
        "expectedVersion": expected_version_str,
    }

    # Use raw dict to avoid alias issues
    update_result = await ctx.java.update_draft(
        resolved_id,
        update_request,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )

    # Handle DRAFT_VERSION_CONFLICT
    if not update_result.ok and update_result.code == "DRAFT_VERSION_CONFLICT":
        return update_result  # Do not auto-override

    if not update_result.ok:
        return update_result

    # Step 7: Verify via GET
    verify_result = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verify_result.ok:
        return ToolResult.internal_error(
            f"Draft {resolved_id} updated but GET verification failed",
            trace_id=trace_id,
        )

    # Step 8: Update session context
    ctx.session.active_draft_id = resolved_id
    ctx.session.record_entity(
        ref=f"draft:{resolved_id}", kind="DRAFT", entity_id=resolved_id,
        label=revised_title, status="READY", run_id=ctx.agent_run_id,
    )

    # Step 9: If active_schedule_id exists, confirm it still points to same draft
    if ctx.session.active_schedule_id:
        schedule_result = await ctx.java.get_schedule(
            ctx.session.active_schedule_id,
            bearer_token=ctx.auth.raw_access_token,
            trace_id=trace_id,
            conversation_id=ctx.conversation_id,
        )
        if schedule_result.ok and schedule_result.data:
            if schedule_result.data.draft_id != resolved_id:
                logger.warning(
                    "Schedule %s draft_id mismatch after revise: expected=%s actual=%s",
                    ctx.session.active_schedule_id, resolved_id,
                    schedule_result.data.draft_id,
                )

    updated = verify_result.data
    if isinstance(updated, DraftResponse):
        return ToolResult.success(
            {
                "draft_id": resolved_id,
                "title": updated.title,
                "content": updated.content,
                "status": updated.status,
                "version": updated.version,
                "updated_at": updated.updated_at.isoformat() if updated.updated_at else None,
            },
            trace_id=trace_id,
        )

    return ToolResult.success(
        verify_result.data.model_dump(mode="json") if hasattr(verify_result.data, "model_dump") else verify_result.data,
        trace_id=trace_id,
    )
