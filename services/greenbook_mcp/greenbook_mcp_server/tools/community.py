"""Community tools — search, read, list posts via Java Agent Facade."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from greenbook_contracts.tool_result import (
    DataProvenance,
    OperationReceipt,
    ResourceRef,
    ToolResult,
)

from ..context import ToolContext

logger = logging.getLogger(__name__)

_INSUFFICIENT_EVIDENCE = "当前社区资料不足"
_GROUNDED_ANSWER_PROMPT = """You answer a community knowledge question from evidence chunks.

Rules:
- Use only the supplied evidence. Do not use general model knowledge.
- If the evidence does not establish the answer, return exactly
  "当前社区资料不足" and an empty sources array.
- Every non-empty answer must cite one or more supplied chunk IDs in sources.
- Never invent a post ID, title, or chunk ID. Sources must use exact supplied IDs.
- Return JSON only with {"answer": string, "sources": [{"postId": string,
  "title": string, "chunkId": string}]}.
"""


def _post_ref(
    post_id: str,
    *,
    title: str | None = None,
    tool: str = "",
) -> ResourceRef:
    return ResourceRef(
        ref=f"post:{post_id}",
        kind="POST",
        resource_id=post_id,
        title=title,
        source=DataProvenance.COMMUNITY_DATA.value,
        tool=tool or None,
    )


def _mark_source(result: ToolResult[Any], source: DataProvenance) -> ToolResult[Any]:
    if not result.ok:
        return result
    return result.model_copy(update={"provenance": [source]})


async def search_public_posts(
    ctx: ToolContext,
    query: str,
    sort: str = "latest",
    page: int = 1,
    size: int = 20,
) -> ToolResult[Any]:
    """Search public posts in the GreenBook community."""
    result = await ctx.java.search_posts(
        query=query,
        sort=sort,
        page=page,
        size=size,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data is not None:
        refs = [
            _post_ref(
                str(getattr(item, "post_id", "") or ""),
                title=getattr(item, "title", None),
                tool="community.search_public_posts",
            )
            for item in getattr(result.data, "items", ())
            if str(getattr(item, "post_id", "") or "")
        ]
        result = result.model_copy(update={"resource_refs": refs})
    return _mark_source(result, DataProvenance.COMMUNITY_DATA)


async def answer_from_knowledge(
    ctx: ToolContext,
    question: str,
    top_posts: int = 8,
    top_chunks: int = 8,
) -> ToolResult[Any]:
    """Retrieve post-scoped evidence, then answer only from those chunks."""
    result = await ctx.java.retrieve_knowledge_evidence(
        question,
        top_posts=top_posts,
        top_chunks=top_chunks,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not result.ok or result.data is None:
        return _mark_source(result, DataProvenance.COMMUNITY_DATA)

    evidence = list(result.data.chunks or [])
    if not evidence:
        return ToolResult.success(
            {"answer": _INSUFFICIENT_EVIDENCE, "sources": []},
            trace_id=result.trace_id,
            provenance=[DataProvenance.COMMUNITY_DATA],
        )
    if ctx.llm is None:
        return ToolResult.failure(
            "GENERATION_UNAVAILABLE",
            "Grounded answer generation requires the configured host LLM",
            "当前暂时无法生成基于社区资料的回答。",
            retryable=True,
            trace_id=result.trace_id,
        )

    evidence_payload = [
        {
            "chunkId": chunk.chunk_id,
            "postId": chunk.post_id,
            "title": chunk.title or "",
            "content": chunk.content,
            "startOffset": chunk.start_offset,
            "endOffset": chunk.end_offset,
        }
        for chunk in evidence
    ]
    try:
        from greenbook_agent_core.llm_compat import structured_call

        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "sources": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "postId": {"type": "string"},
                            "title": {"type": "string"},
                            "chunkId": {"type": "string"},
                        },
                        "required": ["postId", "title", "chunkId"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["answer", "sources"],
            "additionalProperties": False,
        }
        generation_started = time.perf_counter()
        response = await structured_call(
            ctx.llm,
            ctx.model or "deepseek-chat",
            _GROUNDED_ANSWER_PROMPT,
            "community_grounded_answer",
            schema,
            {"question": question, "evidence": evidence_payload},
        )
        payload = _grounded_payload(response)
        answer = str(payload.get("answer") or "").strip() if isinstance(payload, dict) else ""
        raw_sources = payload.get("sources") if isinstance(payload, dict) else []
        if answer == _INSUFFICIENT_EVIDENCE:
            sources = []
        else:
            sources = _validated_sources(raw_sources, evidence)
            if not answer or not sources:
                answer = _INSUFFICIENT_EVIDENCE
                sources = []
        refs = [
            ResourceRef(
                ref=f"post:{chunk.post_id}:chunk:{chunk.chunk_id}",
                kind="POST_CHUNK",
                resource_id=chunk.chunk_id,
                title=chunk.title,
                version=chunk.event_version,
                source=DataProvenance.COMMUNITY_DATA.value,
                tool="community.answer_from_knowledge",
            )
            for chunk in evidence
            if any(source["chunkId"] == chunk.chunk_id for source in sources)
        ]
        answer_result = ToolResult.success(
            {"answer": answer, "sources": sources},
            trace_id=result.trace_id,
            resource_refs=refs,
            provenance=[DataProvenance.COMMUNITY_DATA, DataProvenance.MODEL_INFERENCE],
        )
        answer_result.state = {
            "evidence_count": len(evidence),
            "candidate_post_count": result.data.candidate_post_count,
            "embedding_latency_ms": result.data.embedding_latency_ms,
            "chunk_retrieval_latency_ms": result.data.chunk_retrieval_latency_ms,
            "generation_latency_ms": round((time.perf_counter() - generation_started) * 1000, 3),
        }
        return answer_result
    except Exception as exc:  # fail closed on malformed or unavailable generation
        logger.warning("grounded_answer_generation_failed", exc_info=True)
        return ToolResult.failure(
            "GENERATION_FAILED",
            str(exc)[:500],
            "当前暂时无法生成基于社区资料的回答。",
            retryable=True,
            trace_id=result.trace_id,
        )


def _grounded_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, dict):
        return content
    if not isinstance(content, str) or not content.strip():
        return None
    match = re.search(r"\{.*\}", content.strip(), re.DOTALL)
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _validated_sources(raw_sources: Any, evidence: list[Any]) -> list[dict[str, str]]:
    by_chunk = {chunk.chunk_id: chunk for chunk in evidence}
    if not isinstance(raw_sources, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in raw_sources:
        if not isinstance(raw, dict):
            continue
        chunk_id = str(raw.get("chunkId") or raw.get("chunk_id") or "")
        chunk = by_chunk.get(chunk_id)
        if chunk is None or chunk_id in seen:
            continue
        seen.add(chunk_id)
        result.append({
            "postId": chunk.post_id,
            "title": chunk.title or "",
            "chunkId": chunk.chunk_id,
        })
    return result


async def get_post(
    ctx: ToolContext,
    post_id: str,
) -> ToolResult[Any]:
    """Get a single post by ID."""
    result = await ctx.java.get_post(
        post_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        post_id = str(getattr(result.data, "post_id", "") or "")
        refs = (
            [_post_ref(
                post_id,
                title=getattr(result.data, "title", None),
                tool="community.get_post",
            )]
            if post_id
            else []
        )
        return ToolResult.success(
            result.data.model_dump(mode="json"),
            trace_id=result.trace_id,
            resource_refs=refs,
            provenance=[DataProvenance.COMMUNITY_DATA],
        )
    return _mark_source(result, DataProvenance.COMMUNITY_DATA)


async def list_own_posts(
    ctx: ToolContext,
    page: int = 1,
    size: int = 20,
) -> ToolResult[Any]:
    """List current user's own posts."""
    result = await ctx.java.list_own_posts(
        page=page,
        size=size,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if result.ok and result.data:
        items = [item.model_dump(mode="json") for item in result.data]
        refs = [
            _post_ref(
                str(getattr(item, "post_id", "") or ""),
                title=getattr(item, "title", None),
                tool="community.list_own_posts",
            )
            for item in result.data
            if str(getattr(item, "post_id", "") or "")
        ]
        return ToolResult.success(
            items,
            trace_id=result.trace_id,
            resource_refs=refs,
            provenance=[DataProvenance.PERSONAL_DATA],
        )
    return _mark_source(result, DataProvenance.PERSONAL_DATA)


async def delete_post(
    ctx: ToolContext,
    post_id: str,
) -> ToolResult[Any]:
    """Delete one owned post after approval and verify Java truth."""
    if not ctx.approval_granted:
        return ToolResult.failure(
            "APPROVAL_REQUIRED",
            "community.delete_post requires explicit user approval",
            "Deleting a post needs your confirmation.",
            request_sent=False,
        )

    current = await ctx.java.get_post(
        post_id,
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not current.ok or current.data is None:
        return current
    owner_id = str(getattr(current.data, "author_id", "") or "")
    if owner_id and owner_id != str(ctx.auth.user_id):
        return ToolResult.permission_denied("Post is not owned by the authenticated user")

    key = ctx.idempotency_key("delete_post", scope=post_id)
    result = await ctx.java.delete_post(
        post_id,
        bearer_token=ctx.auth.raw_access_token,
        idempotency_key=key,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
        agent_run_id=ctx.agent_run_id,
        tool_call_id=ctx.tool_call_id,
    )
    if not result.ok:
        return result

    verified = await ctx.java.list_own_posts(
        bearer_token=ctx.auth.raw_access_token,
        trace_id=ctx.trace_id,
        conversation_id=ctx.conversation_id,
    )
    if not verified.ok:
        return ToolResult.failure(
            "RESULT_UNKNOWN",
            f"Post {post_id} deletion was accepted but is still visible",
            "The post deletion is still being verified.",
            request_sent=True,
            trace_id=ctx.trace_id,
        ).model_copy(update={
            "resource_refs": [_post_ref(post_id)],
            "operation_receipt": OperationReceipt(
                operation_id=key,
                semantic_action="DELETE_POST",
                resource_ref=_post_ref(post_id),
                idempotency_key=key,
                request_sent=True,
                downstream_accepted=True,
                side_effect_started=True,
                result_known=False,
                status="RESULT_UNKNOWN",
            ),
        })
    if any(str(getattr(item, "post_id", "")) == post_id for item in (verified.data or [])):
        return ToolResult.failure(
            "RESULT_UNKNOWN",
            f"Post {post_id} deletion could not be verified",
            "The post deletion is still being verified.",
            request_sent=True,
            trace_id=ctx.trace_id,
        )

    ctx.session.record_entity(
        ref=f"post:{post_id}",
        kind="POST",
        entity_id=post_id,
        label=getattr(current.data, "title", None),
        status="DELETED",
        run_id=ctx.agent_run_id,
    )
    return ToolResult.success(
        {"post_id": post_id, "status": "deleted"},
        trace_id=ctx.trace_id,
        receipt_id=result.receipt_id,
        resource_refs=[_post_ref(post_id)],
        provenance=[DataProvenance.PERSONAL_DATA],
        operation_receipt=OperationReceipt(
            operation_id=key,
            semantic_action="DELETE_POST",
            resource_ref=_post_ref(post_id),
            idempotency_key=key,
            request_sent=True,
            downstream_accepted=True,
            side_effect_started=True,
            result_known=True,
            observed_state={"post_id": post_id, "status": "deleted"},
            verification_evidence={"list_own_posts_excludes_post": True},
            status="COMPLETED",
        ),
    )
