"""Content tools — create and revise drafts via Creator Service + Java Facade.

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

import json
import logging
from datetime import UTC, datetime
from typing import Any

from greenbook_contracts.tool_result import ResourceRef, ToolResult
from greenbook_creator_client.client import extract_creator_document
from greenbook_java_client.models import (
    AgentDraftCreateRequest,
    AgentDraftUpdateRequest,
    DraftResponse,
)

from ..context import ToolContext

logger = logging.getLogger(__name__)


def _latest_creator_artifact_id(ctx: ToolContext) -> str | None:
    """Resolve the latest Creator artifact in the existing session projection."""

    candidates = [
        entity
        for entity in ctx.session.recent_entities
        if entity.kind == "CREATOR_ARTIFACT" and entity.entity_id
    ]
    if not candidates:
        return None
    latest = max(
        candidates,
        key=lambda entity: entity.timestamp or datetime.min.replace(tzinfo=UTC),
    )
    return latest.entity_id


def normalize_text_artifact(
    document: dict[str, str | None],
    *,
    fallback_title: str,
    fallback_content: str,
    fallback_summary: str | None = None,
) -> AgentDraftCreateRequest | None:
    """Map a Creator document to the Java text-only draft contract.

    Creator artifacts may contain provider-specific media metadata.  The Java
    Agent Facade draft request intentionally receives only text fields here;
    no local path, artifact id, cover, or upload flag can cross this boundary.
    """
    title = str(document.get("title") or fallback_title).strip()[:256]
    content = str(document.get("body_markdown") or fallback_content).strip()
    summary_value = document.get("description") or fallback_summary
    summary = str(summary_value).strip()[:200] if summary_value else None
    if not title or not content:
        return None
    return AgentDraftCreateRequest(title=title, content=content, summary=summary)


def _reference_notes(references: list[dict[str, Any]] | None) -> str:
    """Serialize only trusted reference fields into a Creator brief."""

    compact = [
        {
            "id": str(r.get("post_id") or r.get("id", ""))[:256],
            "title": str(r.get("title", ""))[:200],
            "summary": str(r.get("description") or r.get("summary", ""))[:600],
            "body_excerpt": str(r.get("body") or r.get("body_markdown", ""))[:1600],
        }
        for r in (references or [])[:8]
        if r.get("post_id") or r.get("id")
    ]
    return "\n\n".join(
        f"[{item['id']}] {item['title']}\n{item['summary']}\n{item['body_excerpt']}"
        for item in compact
    )[:12_000]


async def build_strategy(
    ctx: ToolContext,
    instruction: str,
    references: list[dict[str, Any]] | None = None,
    constraints: dict[str, Any] | None = None,
) -> ToolResult[Any]:
    """Run Creator's existing BUILD_STRATEGY task without creating a Java draft."""

    trace_id = ctx.trace_id
    creator_result = await ctx.creator.create_task(
        kind="BUILD_STRATEGY",
        goal=instruction,
        constraints={
            "interaction_mode": "AUTO",
            "format": "POST",
            "tone": "PRACTICAL",
            "audience": "GreenBook community users",
            **(constraints or {}),
        },
        reference_notes=_reference_notes(references),
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=ctx.idempotency_key(
            "build_strategy",
            scope=f"{instruction}|{json.dumps(constraints or {}, sort_keys=True)}",
        ),
        trace_id=trace_id,
    )
    if not creator_result.ok:
        return creator_result

    task_id = str((creator_result.data or {}).get("task_id", ""))
    if not task_id:
        return ToolResult.internal_error(
            "Creator returned a strategy task without task_id",
            trace_id=trace_id,
        )

    wait_result = await ctx.creator.wait_for_completion(
        task_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
    )
    if not wait_result.ok:
        return wait_result

    snapshot = wait_result.data or {}
    artifact_id = str(snapshot.get("final_artifact_id") or "")
    if not artifact_id:
        return ToolResult.creator_unavailable(
            "Creator completed without a strategy artifact",
            trace_id=trace_id,
        )

    artifact_result = await ctx.creator.get_artifact(
        task_id,
        artifact_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
    )
    if not artifact_result.ok:
        return artifact_result

    artifact = dict(artifact_result.data or {})
    artifact_kind = str(artifact.get("kind") or "TOPIC_OPTIONS")
    strategy = artifact.get("content") or {}
    ctx.session.record_entity(
        ref=f"creator-task:{task_id}",
        kind="CREATOR_TASK",
        entity_id=task_id,
        label="Creator content strategy",
        status="COMPLETED",
        run_id=ctx.agent_run_id,
    )
    ctx.session.record_entity(
        ref=f"artifact:{artifact_id}",
        kind="CREATOR_ARTIFACT",
        entity_id=artifact_id,
        label="Creator content strategy",
        status="COMPLETED",
        run_id=ctx.agent_run_id,
    )
    return ToolResult.success(
        {
            "creator_task_id": task_id,
            "creator_artifact_id": artifact_id,
            "artifact_kind": artifact_kind,
            "strategy": strategy,
        },
        trace_id=trace_id,
        receipt_id=creator_result.receipt_id,
        resource_refs=[
            ResourceRef(ref=f"creator-task:{task_id}", kind="CREATOR_TASK", resource_id=task_id),
            ResourceRef(
                ref=f"artifact:{artifact_id}",
                kind="CREATOR_ARTIFACT",
                resource_id=artifact_id,
            ),
        ],
    )


async def create_draft(
    ctx: ToolContext,
    title: str,
    instruction: str,
    references: list[dict[str, Any]] | None = None,
    strategy_task_id: str | None = None,
    strategy_artifact_id: str | None = None,
    summary: str | None = None,
) -> ToolResult[Any]:
    """Create a new draft via Creator Service → Java Facade."""
    trace_id = ctx.trace_id
    refs = references or []

    # Build reference notes from search results and, when the plan contains a
    # Creator strategy step, fetch that exact artifact rather than asking the
    # Creator to reconstruct strategy from the user message.
    reference_notes = _reference_notes(refs)
    if bool(strategy_task_id) != bool(strategy_artifact_id):
        return ToolResult.validation_error(
            "A strategy_task_id and strategy_artifact_id must be supplied together.",
            user_message="创作步骤缺少完整的策略产物引用。",
        )
    if strategy_task_id and strategy_artifact_id:
        strategy_result = await ctx.creator.get_artifact(
            strategy_task_id,
            strategy_artifact_id,
            bearer_token=ctx.auth.raw_access_token,
            trace_id=trace_id,
        )
        if not strategy_result.ok:
            return strategy_result
        strategy_payload = (strategy_result.data or {}).get("content")
        if isinstance(strategy_payload, dict):
            strategy_notes = json.dumps(
                strategy_payload,
                ensure_ascii=False,
                sort_keys=True,
            )[:12_000]
            reference_notes = (
                f"{reference_notes}\n\n" if reference_notes else ""
            ) + "Creator strategy artifact:\n" + strategy_notes

    # Step 1: Submit Creator task with AUTO interaction mode
    idempotency_key = ctx.idempotency_key(
        "create_draft",
        scope=(
            f"{title}|{instruction}|{summary or ''}|"
            f"{strategy_task_id or ''}|{strategy_artifact_id or ''}"
        ),
    )

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

    creator_data = creator_result.data or {}
    task_id = str(creator_data.get("task_id", ""))
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

    if not final_artifact_id:
        return ToolResult.creator_unavailable(
            "Creator completed without a final artifact",
            trace_id=trace_id,
        )

    if final_artifact_id:
        artifact_result = await ctx.creator.get_artifact(
            task_id, final_artifact_id,
            bearer_token=ctx.auth.raw_access_token,
            trace_id=trace_id,
        )
        if not artifact_result.ok:
            return artifact_result
        doc = extract_creator_document(artifact_result.data or {})
        document = {
            "title": doc["title"] or title,
            "description": doc["description"] or summary,
            "body_markdown": doc["body_markdown"] or instruction,
        }
        creator_artifact_id = final_artifact_id

    # Step 4: Call Java Agent Facade POST /drafts.  Creator publication
    # handoff is intentionally not used here: that endpoint creates a
    # second Java draft, which would make one Agent operation produce
    # duplicate drafts.  The Agent owns this single handoff to the
    # Java Agent Facade; Creator remains the content-generation service.
    java_create = normalize_text_artifact(
        document,
        fallback_title=title,
        fallback_content=instruction,
        fallback_summary=summary,
    )
    if java_create is None:
        return ToolResult.validation_error(
            "Creator returned an empty text document.",
            user_message="创作结果缺少标题或正文，未创建草稿。",
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

    # Step 5: Verify via GET /drafts/{draftId}
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

    # Step 6: Update SessionContext
    ctx.session.active_draft_id = draft_id
    ctx.session.record_entity(
        ref=f"draft:{draft_id}", kind="DRAFT", entity_id=draft_id,
        label=draft.title, status="READY", run_id=ctx.agent_run_id,
    )
    if creator_artifact_id:
        ctx.session.record_entity(
            ref=f"artifact:{creator_artifact_id}",
            kind="CREATOR_ARTIFACT",
            entity_id=creator_artifact_id,
            label=document.get("title") or draft.title,
            status="COMPLETED",
            run_id=ctx.agent_run_id,
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
            "strategy_task_id": strategy_task_id,
            "strategy_artifact_id": strategy_artifact_id,
        },
        trace_id=trace_id,
        receipt_id=draft_result.receipt_id,
        resource_refs=resource_refs,
    )


async def get_draft(
    ctx: ToolContext,
    draft_id: str | None = None,
) -> ToolResult[Any]:
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
) -> ToolResult[Any]:
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
    draft_id: str,
    revision_instruction: str,
    title: str | None = None,
    revision_scope: str = "FULL_REVISION",
    expected_version: str | None = None,
) -> ToolResult[Any]:
    """Revise an existing draft via Creator Service → Java Facade.

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
    source_creator_artifact_id = _latest_creator_artifact_id(ctx)
    normalized_scope = str(revision_scope or "FULL_REVISION").strip().upper()
    if normalized_scope == "FULL_REVISION" and title:
        instruction_text = str(revision_instruction or "").lower()
        if "标题" in instruction_text or "title" in instruction_text:
            normalized_scope = "TITLE_ONLY"

    # Step 3: Submit Creator revise task
    idempotency_key = ctx.idempotency_key(
        "revise_draft",
        scope=f"{resolved_id}|{revision_instruction}",
    )

    creator_result = await ctx.creator.create_task(
        kind="IMPROVE_DRAFT",
        goal=f"Revise the following draft: {revision_instruction}",
        constraints={
            "interaction_mode": "AUTO",
            "format": "POST",
            "target_length": 1200,
            "tone": "PRACTICAL",
            "revision_scope": normalized_scope,
            "revision_instruction": revision_instruction,
            "requested_title": title,
            "source_artifact_id": source_creator_artifact_id,
            "draft": {
                "title": current_title,
                "body_markdown": current_content,
                "source_artifact_id": source_creator_artifact_id,
            },
        },
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    if not creator_result.ok:
        return creator_result

    creator_data = creator_result.data or {}
    task_id = str(creator_data.get("task_id", ""))
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

    # Step 5: Get revised content.  A completed Creator task without a final
    # artifact is a dependency failure; never use the user's instruction as
    # the new post body.
    if not final_artifact_id:
        return ToolResult.creator_unavailable(
            "Creator completed without a final revision artifact",
            trace_id=trace_id,
        )

    artifact_result = await ctx.creator.get_artifact(
        task_id,
        final_artifact_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
    )
    if not artifact_result.ok:
        return artifact_result

    doc = extract_creator_document(artifact_result.data or {})
    revised_title = doc["title"] or title or current_title
    revised_content = doc["body_markdown"]
    revised_summary = doc["description"]
    if not revised_content:
        return ToolResult.validation_error(
            "Creator returned an empty revised document",
            user_message="创作服务没有返回有效的修改后正文，本次尚未保存修改。",
        )

    # Step 6: Re-read the draft after the potentially long Creator operation.
    # The Java response is the only canonical optimistic-lock version.  Never
    # let a model-produced ``expected_version`` override it: model output can
    # contain a locally invented timestamp or precision that Java does not
    # persist.  Comparing the two Java reads still rejects a real concurrent
    # edit while allowing the normal Creator-backed revision to proceed.
    latest = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not latest.ok:
        return latest
    latest_data = latest.data
    if not isinstance(latest_data, DraftResponse):
        return ToolResult.internal_error(
            "Unexpected draft response type while checking revision version",
            trace_id=trace_id,
        )

    latest_updated_at = latest_data.updated_at
    if current_updated_at != latest_updated_at:
        return ToolResult.conflict(
            "DRAFT_VERSION_CONFLICT: the draft changed while Creator was revising it; "
            "the revision was not applied.",
            trace_id=trace_id,
        )

    expected_version_str = latest_updated_at.isoformat() if latest_updated_at else None
    if expected_version and expected_version != expected_version_str:
        logger.warning(
            "Ignoring non-canonical model expected_version for draft %s; "
            "using Java updatedAt=%s",
            resolved_id,
            expected_version_str,
        )

    update_request = AgentDraftUpdateRequest(
        title=revised_title,
        content=revised_content,
        summary=revised_summary,
        expectedVersion=expected_version_str,
    )

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
    ctx.session.record_entity(
        ref=f"artifact:{final_artifact_id}",
        kind="CREATOR_ARTIFACT",
        entity_id=final_artifact_id,
        label=revised_title,
        status="COMPLETED",
        run_id=ctx.agent_run_id,
    )

    # Step 9: If active_schedule_id exists, confirm it still points to same draft
    if ctx.session.active_schedule_id:
        schedule_result = await ctx.java.get_schedule(
            ctx.session.active_schedule_id,
            bearer_token=ctx.auth.raw_access_token,
            trace_id=trace_id,
            conversation_id=ctx.conversation_id,
        )
        if (
            schedule_result.ok
            and schedule_result.data
            and schedule_result.data.draft_id != resolved_id
        ):
            logger.warning(
                "Schedule %s draft_id mismatch after revise: expected=%s actual=%s",
                ctx.session.active_schedule_id, resolved_id,
                schedule_result.data.draft_id,
            )

    updated = verify_result.data
    if isinstance(updated, DraftResponse):
        resource_refs = [
            ResourceRef(
                ref=f"draft:{resolved_id}",
                kind="DRAFT",
                resource_id=resolved_id,
                version=updated.version,
            ),
            ResourceRef(
                ref=f"task:{task_id}",
                kind="CREATOR_TASK",
                resource_id=task_id,
            ),
            ResourceRef(
                ref=f"artifact:{final_artifact_id}",
                kind="CREATOR_ARTIFACT",
                resource_id=final_artifact_id,
            ),
        ]
        if source_creator_artifact_id:
            resource_refs.append(
                ResourceRef(
                    ref=f"artifact-parent:{source_creator_artifact_id}",
                    kind="CREATOR_ARTIFACT_PARENT",
                    resource_id=source_creator_artifact_id,
                )
            )
        return ToolResult.success(
            {
                "draft_id": resolved_id,
                "title": updated.title,
                "content": updated.content,
                "status": updated.status,
                "version": updated.version,
                "updated_at": updated.updated_at.isoformat() if updated.updated_at else None,
                "creator_task_id": task_id,
                "creator_artifact_id": final_artifact_id,
                "source_creator_artifact_id": source_creator_artifact_id,
                "revision_scope": normalized_scope,
            },
            trace_id=trace_id,
            resource_refs=resource_refs,
        )

    verified_data = verify_result.data
    if verified_data is None:
        return ToolResult.result_unknown("Updated draft could not be verified", trace_id=trace_id)
    payload = (
        verified_data.model_dump(mode="json")
        if hasattr(verified_data, "model_dump")
        else verified_data
    )
    return ToolResult.success(payload, trace_id=trace_id)
