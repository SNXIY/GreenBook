"""Content tools — create and manage drafts via the Java Agent Facade.

content.create_draft (assistant-first, lightweight):
  1. Receive topic + optional reference posts
  2. Generate the draft body with the host LLM in one round trip
  3. Call Java POST /drafts with Idempotency-Key
  4. Update SessionContext.active_draft_id

content.get_draft / list_drafts: read drafts from the Java facade.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from greenbook_contracts.tool_result import OperationReceipt, ResourceRef, ToolResult
from greenbook_java_client.models import (
    DESCRIPTION_MAX_LENGTH,
    AgentDraftCreateRequest,
    AgentDraftUpdateRequest,
    DraftResponse,
)
from pydantic import ValidationError

from ..context import ToolContext

logger = logging.getLogger(__name__)


def _draft_ref(draft_id: str, version: int | None = None) -> ResourceRef:
    return ResourceRef(
        ref=f"draft:{draft_id}",
        kind="DRAFT",
        resource_id=draft_id,
        version=version,
    )


def _operation_receipt(
    *,
    operation_id: str,
    semantic_action: str,
    draft: DraftResponse | None = None,
    result_known: bool,
    status: str,
    observed_state: dict[str, Any] | None = None,
    verification_evidence: dict[str, Any] | None = None,
    request_sent: bool = True,
    downstream_accepted: bool = True,
    side_effect_started: bool = True,
) -> OperationReceipt:
    """Build the common evidence envelope for a draft write.

    The ToolResult's ``ok`` is therefore reserved for verified business
    postconditions, not merely for a Java response body being received.
    """

    ref = _draft_ref(draft.draft_id, draft.version) if draft is not None else None
    return OperationReceipt(
        operation_id=operation_id,
        semantic_action=semantic_action,
        resource_ref=ref,
        idempotency_key=operation_id,
        request_sent=request_sent,
        downstream_accepted=downstream_accepted,
        side_effect_started=side_effect_started,
        result_known=result_known,
        observed_state=observed_state,
        verification_evidence=verification_evidence,
        status=status,
    )


def _draft_payload(draft: DraftResponse) -> dict[str, Any]:
    return {
        "draft_id": draft.draft_id,
        "title": draft.title,
        "content": draft.content,
        "summary": draft.summary,
        "status": draft.status,
        "version": draft.version,
        "updated_at": draft.updated_at.isoformat() if draft.updated_at else None,
    }


def _unknown_after_write(
    *,
    ctx: ToolContext,
    operation_id: str,
    semantic_action: str,
    draft: DraftResponse | None,
    message: str,
    verification_evidence: dict[str, Any] | None = None,
    receipt_id: str | None = None,
) -> ToolResult[Any]:
    """Return a reconciliation-required result after an accepted write."""

    refs = [_draft_ref(draft.draft_id, draft.version)] if draft is not None else []
    receipt = _operation_receipt(
        operation_id=operation_id,
        semantic_action=semantic_action,
        draft=draft,
        result_known=False,
        status="RESULT_UNKNOWN",
        observed_state=_draft_payload(draft) if draft is not None else None,
        verification_evidence=verification_evidence,
    )
    return ToolResult.result_unknown(
        message,
        trace_id=ctx.trace_id,
        receipt_id=receipt_id,
        resource_refs=refs,
        operation_receipt=receipt,
        state={
            "semantic_action": semantic_action,
            "operation_id": operation_id,
            "downstream_accepted": True,
            "side_effect_started": True,
            "result_known": False,
        },
    )




def normalize_text_artifact(
    document: dict[str, str | None],
    *,
    fallback_title: str,
    fallback_content: str,
    fallback_summary: str | None = None,
) -> AgentDraftCreateRequest | None:
    """Map a generated document to the Java text-only draft contract.

    Generated artifacts may carry provider-specific media metadata.  The Java
    Agent Facade draft request intentionally receives only text fields here;
    no local path, artifact id, cover, or upload flag can cross this boundary.
    """
    title = str(document.get("title") or fallback_title).strip()
    content = str(document.get("body_markdown") or fallback_content).strip()
    summary_value = document.get("description") or fallback_summary
    summary = str(summary_value).strip() if summary_value else None
    if not title or not content:
        return None
    return AgentDraftCreateRequest(title=title, content=content, summary=summary)


def _reference_notes(references: list[dict[str, Any]] | None) -> str:
    """Serialize only trusted reference fields into the generation brief."""

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


_DRAFT_WRITE_PROMPT = """你是一位中文技术社区（GreenBook 知光）的内容作者。

根据「创作要求」与可选的「参考笔记」直接写出一篇完整的帖子正文。要求：
- 用中文撰写，结构清晰（可含小标题与列表），篇幅 800~1500 字；
- 基于参考笔记提炼观点时保持真实，不编造引用中没有的事实；
- 直接输出一篇可发布的成稿，不要提问、不要解释创作过程。
只返回一个 JSON 对象：{"title": 标题, "body_markdown": 正文}，不要输出其他内容。
"""


async def _direct_generate(
    ctx: ToolContext,
    *,
    title: str,
    instruction: str,
    reference_notes: str,
    summary: str | None,
) -> dict[str, str] | None:
    """Generate a complete draft body in one host-LLM round trip.

    Assistant-first lightweight path: no creator profile / strategy /
    outline / review pipeline.  Falls back to None on any failure so the
    caller receives a clean failure.
    """
    if ctx.llm is None:
        return None
    try:
        from greenbook_agent_core.llm_compat import structured_call
        from greenbook_agent_core.observability.run_metrics import llm_category_scope

        schema = {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body_markdown": {"type": "string"},
            },
            "required": ["title", "body_markdown"],
            "additionalProperties": False,
        }
        with llm_category_scope("CREATOR"):
            response = await structured_call(
                ctx.llm,
                ctx.model or "deepseek-chat",
                _DRAFT_WRITE_PROMPT,
                "greenbook_draft",
                schema,
                {
                    "instruction": instruction,
                    "title_hint": title,
                    "summary": summary or "",
                    "reference_notes": reference_notes[:8000] or "",
                },
            )
        payload = _draft_response_payload(response)
        if not isinstance(payload, dict):
            return None
        body = str(payload.get("body_markdown") or payload.get("content") or "").strip()
        generated_title = str(payload.get("title") or "").strip()
        if not body:
            return None
        return {
            "title": generated_title or title,
            "body_markdown": body,
        }
    except Exception:
        logger.warning("direct_draft_generation_failed", exc_info=True)
        return None


def _draft_response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return None
    import re as _re

    text = content.strip()
    match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if match is None:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


async def _save_draft_via_java(
    ctx: ToolContext,
    *,
    title: str,
    instruction: str,
    summary: str | None,
    document: dict[str, str | None],
    idempotency_key: str,
    trace_id: str,
) -> ToolResult[Any]:
    """Persist a generated document through the Java Agent Facade."""
    try:
        java_create = normalize_text_artifact(
            document,
            fallback_title=title,
            fallback_content=instruction,
            fallback_summary=(
                summary
                if summary is not None and len(summary) <= DESCRIPTION_MAX_LENGTH
                else None
            ),
        )
    except ValidationError as exc:
        error = exc.errors()[0] if exc.errors() else {}
        field = str((error.get("loc") or ["summary"])[0])
        return ToolResult.permanent_input(
            "FIELD_TOO_LONG",
            f"{field} exceeds {DESCRIPTION_MAX_LENGTH} characters",
            user_message="生成的草稿信息不符合发布要求，系统无法保存草稿。",
            state={
                "field": field,
                "max_length": DESCRIPTION_MAX_LENGTH,
                "actual_length": len(str(document.get("description") or summary or "")),
                "boundary": "python_java_contract",
            },
        )
    if java_create is None:
        return ToolResult.validation_error(
            "Generated document has no title or body.",
            user_message="生成结果缺少标题或正文，未创建草稿。",
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

    verified_result = await ctx.java.get_draft(
        draft.draft_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verified_result.ok or not isinstance(verified_result.data, DraftResponse):
        return _unknown_after_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CREATE_DRAFT",
            draft=draft,
            message=f"Draft {draft.draft_id} was created but could not be verified",
            receipt_id=draft_result.receipt_id,
            verification_evidence={"get_draft_ok": verified_result.ok},
        )
    verified = verified_result.data
    if (
        verified.draft_id != draft.draft_id
        or verified.status != "draft"
        or verified.title != java_create.title
        or verified.content != java_create.content
    ):
        return _unknown_after_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="CREATE_DRAFT",
            draft=verified,
            message=f"Draft {draft.draft_id} postcondition verification mismatch",
            receipt_id=draft_result.receipt_id,
            verification_evidence={
                "expected_title": java_create.title,
                "actual_title": verified.title,
                "expected_content": java_create.content,
                "actual_content": verified.content,
                "expected_status": "draft",
                "actual_status": verified.status,
            },
        )

    ctx.session.active_draft_id = draft.draft_id
    ctx.session.record_entity(
        ref=f"draft:{draft.draft_id}", kind="DRAFT", entity_id=draft.draft_id,
        label=draft.title, status="READY", run_id=ctx.agent_run_id,
    )
    receipt = _operation_receipt(
        operation_id=idempotency_key,
        semantic_action="CREATE_DRAFT",
        draft=verified,
        result_known=True,
        status="COMPLETED",
        observed_state=_draft_payload(verified),
        verification_evidence={
            "draft_id_matches": True,
            "status": verified.status,
            "title_matches": True,
            "content_matches": True,
        },
    )
    payload = _draft_payload(verified)
    payload["generation"] = "assistant_direct"
    return ToolResult.success(
        payload,
        trace_id=trace_id,
        receipt_id=draft_result.receipt_id,
        resource_refs=[_draft_ref(verified.draft_id, verified.version)],
        operation_receipt=receipt,
    )



async def create_draft(
    ctx: ToolContext,
    title: str,
    instruction: str,
    references: list[dict[str, Any]] | None = None,
    summary: str | None = None,
) -> ToolResult[Any]:
    """Create a new draft: one host-LLM call writes the body, then Java persists.

    Lightweight assistant-first design (prompt + model + MCP): no separate creator service, no profile / strategy / outline / review pipeline.
    """
    trace_id = ctx.trace_id
    reference_notes = _reference_notes(references or [])
    idempotency_key = ctx.idempotency_key(
        "create_draft",
        scope=f"{title}|{instruction}|{summary or ''}",
    )
    document = await _direct_generate(
        ctx,
        title=title,
        instruction=instruction,
        reference_notes=reference_notes,
        summary=summary,
    )
    if document is None:
        return ToolResult.internal_error(
            "Draft generation produced no document", trace_id=trace_id
        )
    return await _save_draft_via_java(
        ctx,
        title=title,
        instruction=instruction,
        summary=summary,
        document=document,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
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


async def update_draft(
    ctx: ToolContext,
    draft_id: str | None = None,
    title: str | None = None,
    content: str | None = None,
) -> ToolResult[Any]:
    """Partially update one draft and verify only the requested mutation.

    Fields omitted by the user are deliberately not included in the Java
    request, so a title-only or body-only request cannot overwrite the other
    field with planner/session data.
    """

    resolved_id = draft_id or ctx.session.active_draft_id
    if not resolved_id:
        resolved_id, candidates = ctx.session.resolve_active_draft_id()
        if not resolved_id and candidates:
            return ToolResult.validation_error(
                "Multiple drafts found; an explicit draft target is required.",
                user_message="There are multiple drafts in this conversation. Please specify which draft to update.",
            )
        if not resolved_id:
            return ToolResult.not_found("No draft to update.")

    current_result = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not current_result.ok or not isinstance(current_result.data, DraftResponse):
        return current_result
    current = current_result.data
    if current.status != "draft":
        return ToolResult.business_rejected(
            f"Draft {resolved_id} is in status {current.status}",
            user_message="Only a draft can be edited.",
        )
    if current.updated_at is None:
        return ToolResult.failure(
            "DRAFT_VERSION_UNAVAILABLE",
            f"Draft {resolved_id} has no authoritative update version",
            "The current draft version could not be confirmed. Please refresh and try again.",
            request_sent=False,
        )

    expected_version = current.updated_at.isoformat()
    idempotency_key = ctx.idempotency_key(
        "update_draft",
        scope=f"{resolved_id}|{expected_version}|{title or ''}|{content or ''}",
    )
    request = AgentDraftUpdateRequest.model_validate(
        {
            "title": title,
            "content": content,
            "expectedVersion": expected_version,
        }
    )
    result = await ctx.java.update_draft(
        resolved_id,
        request,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not result.ok:
        return result

    verified_result = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    returned = result.data if isinstance(result.data, DraftResponse) else current
    if not verified_result.ok or not isinstance(verified_result.data, DraftResponse):
        return _unknown_after_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_DRAFT",
            draft=returned,
            message=f"Draft {resolved_id} was updated but could not be verified",
            receipt_id=result.receipt_id,
            verification_evidence={"get_draft_ok": verified_result.ok},
        )
    verified = verified_result.data
    mismatches: dict[str, Any] = {}
    if verified.draft_id != resolved_id:
        mismatches["draft_id"] = {"expected": resolved_id, "actual": verified.draft_id}
    if verified.status != "draft":
        mismatches["status"] = {"expected": "draft", "actual": verified.status}
    if title is not None and verified.title != title:
        mismatches["title"] = {"expected": title, "actual": verified.title}
    if content is not None and verified.content != content:
        mismatches["content"] = {"expected": content, "actual": verified.content}
    if mismatches:
        return _unknown_after_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="UPDATE_DRAFT",
            draft=verified,
            message=f"Draft {resolved_id} postcondition verification mismatch",
            receipt_id=result.receipt_id,
            verification_evidence=mismatches,
        )

    ctx.session.active_draft_id = resolved_id
    ctx.session.record_entity(
        ref=f"draft:{resolved_id}",
        kind="DRAFT",
        entity_id=resolved_id,
        label=verified.title,
        status="READY",
        run_id=ctx.agent_run_id,
    )
    receipt = _operation_receipt(
        operation_id=idempotency_key,
        semantic_action="UPDATE_DRAFT",
        draft=verified,
        result_known=True,
        status="COMPLETED",
        observed_state=_draft_payload(verified),
        verification_evidence={"requested_fields_match": True},
    )
    return ToolResult.success(
        _draft_payload(verified),
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[_draft_ref(verified.draft_id, verified.version)],
        operation_receipt=receipt,
    )


async def delete_draft(
    ctx: ToolContext,
    draft_id: str | None = None,
) -> ToolResult[Any]:
    """Soft-delete one draft only after approval and authoritative read-back."""

    if not ctx.approval_granted:
        return ToolResult.failure(
            "APPROVAL_REQUIRED",
            "content.delete_draft requires explicit user approval",
            "Deleting a draft needs your confirmation.",
            request_sent=False,
        )

    resolved_id = draft_id or ctx.session.active_draft_id
    if not resolved_id:
        resolved_id, candidates = ctx.session.resolve_active_draft_id()
        if not resolved_id and candidates:
            return ToolResult.validation_error(
                "Multiple drafts found; an explicit draft target is required.",
                user_message="There are multiple drafts in this conversation. Please specify which draft to delete.",
            )
        if not resolved_id:
            return ToolResult.not_found("No draft to delete.")

    current_result = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not current_result.ok or not isinstance(current_result.data, DraftResponse):
        return current_result
    current = current_result.data
    if current.status == "deleted":
        return ToolResult.success(
            {"draft_id": resolved_id, "status": "deleted", "already_deleted": True},
            trace_id=ctx.trace_id,
            resource_refs=[_draft_ref(resolved_id, current.version)],
            operation_receipt=_operation_receipt(
                operation_id=ctx.idempotency_key("delete_draft", scope=resolved_id),
                semantic_action="DELETE_DRAFT",
                draft=current,
                result_known=True,
                status="COMPLETED",
                observed_state=_draft_payload(current),
                verification_evidence={"already_deleted": True},
                request_sent=False,
                downstream_accepted=False,
                side_effect_started=False,
            ),
        )
    if current.status != "draft":
        return ToolResult.business_rejected(
            f"Draft {resolved_id} is in status {current.status}",
            user_message="Only a draft can be deleted.",
        )

    idempotency_key = ctx.idempotency_key("delete_draft", scope=resolved_id)
    result = await ctx.java.delete_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=idempotency_key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not result.ok:
        return result

    verified_result = await ctx.java.get_draft(
        resolved_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verified_result.ok or not isinstance(verified_result.data, DraftResponse):
        return _unknown_after_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="DELETE_DRAFT",
            draft=current,
            message=f"Draft {resolved_id} deletion was accepted but could not be verified",
            receipt_id=result.receipt_id,
            verification_evidence={"get_draft_ok": verified_result.ok},
        )
    verified = verified_result.data
    if verified.status != "deleted":
        return _unknown_after_write(
            ctx=ctx,
            operation_id=idempotency_key,
            semantic_action="DELETE_DRAFT",
            draft=verified,
            message=f"Draft {resolved_id} deletion postcondition verification mismatch",
            receipt_id=result.receipt_id,
            verification_evidence={"expected_status": "deleted", "actual_status": verified.status},
        )

    if ctx.session.active_draft_id == resolved_id:
        ctx.session.active_draft_id = None
    ctx.session.record_entity(
        ref=f"draft:{resolved_id}",
        kind="DRAFT",
        entity_id=resolved_id,
        label=verified.title,
        status="DELETED",
        run_id=ctx.agent_run_id,
    )
    return ToolResult.success(
        {"draft_id": resolved_id, "status": "deleted"},
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[_draft_ref(resolved_id, verified.version)],
        operation_receipt=_operation_receipt(
            operation_id=idempotency_key,
            semantic_action="DELETE_DRAFT",
            draft=verified,
            result_known=True,
            status="COMPLETED",
            observed_state=_draft_payload(verified),
            verification_evidence={"status": "deleted"},
        ),
    )
