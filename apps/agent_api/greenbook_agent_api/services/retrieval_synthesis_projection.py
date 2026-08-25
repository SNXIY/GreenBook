"""Project read evidence into a user-facing retrieval interaction.

The AgentLoop reflection is an internal control signal.  This module is the
small presentation boundary used by the conversation adapter when a read-only
turn has collected community search results and post details.  It deliberately
accepts only structured tool evidence and never uses reflection text as the
answer.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from greenbook_agent_core.llm_compat import (
    add_json_schema_instruction,
    has_structured_payload,
    structured_provider_options,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)

_SEARCH_CAPABILITIES = {"SEARCH_COMMUNITY", "LIST_OWN_POSTS"}
_DETAIL_CAPABILITIES = {"GET_POST_DETAIL"}
_SEARCH_TOOL_MARKERS = ("SEARCH_PUBLIC_POSTS", "LIST_OWN_POSTS")
_DETAIL_TOOL_MARKERS = ("GET_POST",)
_MAX_SOURCES = 5
_MAX_SEARCH_ITEMS = 5
_MAX_EVIDENCE_CHARS = 6000
_READABLE_STATUSES = {"FULL", "PARTIAL"}
_INTERNAL_REFERENCE = re.compile(
    r"\b(?:source|evidence|artifact)[-_][A-Za-z0-9:_-]+\b"
    r"|\b(?:execution|task|plan|goal|run|step)[-_][A-Za-z0-9:_-]+\b"
    r"|\b[0-9a-f]{8}-[0-9a-f-]{27,}\b",
    re.IGNORECASE,
)
_UNVERIFIED_HEDGE = re.compile(
    r"\b(?:may|might|possibly|probably|perhaps|seems|appears)\b"
    r"|(?:可能|大概|应该|似乎|或许)",
    re.IGNORECASE,
)


class _SynthesisPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = ""
    explanation: str = ""
    source_refs: list[str] = Field(default_factory=list)


class _SynthesisDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intro: str = ""
    common_patterns: list[_SynthesisPoint] = Field(default_factory=list)
    differences: list[_SynthesisPoint] = Field(default_factory=list)
    conclusion: str = ""


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _compact(value: Any, limit: int = 220) -> str:
    normalized = re.sub(r"\s+", " ", _text(value)).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _clean_excerpt(value: Any, limit: int = 260) -> str:
    """Make a short, readable source excerpt without leaking markdown debris."""

    normalized = _text(value)
    if not normalized:
        return ""
    normalized = re.sub(r"```(?:markdown|md|text)?", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"`([^`]*)`", r"\1", normalized)
    normalized = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", normalized)
    lines = []
    for line in normalized.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
        line = re.sub(r"^\s*[-*+]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return _compact(" ".join(lines), limit)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _payload(tool_result: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("data", "payload", "result"):
        value = tool_result.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        for key in ("items", "posts", "results", "data", "post", "item"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
            if isinstance(nested, Mapping):
                return [dict(nested)]
        return [dict(value)] if any(
            key in value for key in ("post_id", "postId", "title", "content", "body")
        ) else []
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, Mapping)]
    return []


def _is_success(item: Mapping[str, Any]) -> bool:
    if "ok" not in item and "success" not in item:
        return True
    return bool(item.get("ok", item.get("success", False)))


def _is_search(item: Mapping[str, Any]) -> bool:
    capability = _text(item.get("capability")).upper()
    tool = _text(item.get("tool_name") or item.get("tool")).upper().replace(".", "_")
    return capability in _SEARCH_CAPABILITIES or any(marker in tool for marker in _SEARCH_TOOL_MARKERS)


def _is_detail(item: Mapping[str, Any]) -> bool:
    capability = _text(item.get("capability")).upper()
    tool = _text(item.get("tool_name") or item.get("tool")).upper().replace(".", "_")
    return capability in _DETAIL_CAPABILITIES or any(marker in tool for marker in _DETAIL_TOOL_MARKERS)


def _resource_id(record: Mapping[str, Any]) -> str:
    return _text(
        record.get("post_id")
        or record.get("postId")
        or record.get("resource_id")
        or record.get("id")
    )


def _title(record: Mapping[str, Any]) -> str:
    return _compact(record.get("title") or record.get("name"), 180) or "未命名内容"


def _body(record: Mapping[str, Any]) -> str:
    return _text(
        record.get("content")
        or record.get("body")
        or record.get("body_markdown")
        or record.get("text")
    )


def _summary(record: Mapping[str, Any]) -> str:
    explicit = _text(record.get("summary") or record.get("description") or record.get("excerpt"))
    if explicit:
        return _clean_excerpt(explicit)
    return _clean_excerpt(_body(record))


def _language_is_chinese(request: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", request))


def _language_copy(request: str) -> dict[str, str]:
    if _language_is_chinese(request):
        return {
            "found": "找到",
            "read": "重点阅读了",
            "related": "篇相关内容",
            "insufficient": "目前取得的完整内容不足以可靠归纳共同点。",
            "no_common": "目前没有足够的跨来源证据归纳出共同点。",
            "no_results": "没有找到足够相关的内容。",
            "partial": "部分正文未能读取。",
            "title": "社区内容总结",
        }
    return {
        "found": "Found",
        "read": "Read",
        "related": "related items",
        "insufficient": "There is not enough full-text evidence to reliably identify common points.",
        "no_common": "The retrieved sources do not provide enough cross-source evidence for a reliable common pattern.",
        "no_results": "No sufficiently relevant content was found.",
        "partial": "Some full-text items could not be retrieved.",
        "title": "Community content summary",
    }


def _search_snapshot(tool_results: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int | None, str]:
    for item in tool_results:
        if not _is_search(item) or not _is_success(item):
            continue
        data = _payload(item)
        records = _records(data)
        raw_count = data.get("total") or data.get("count") or data.get("total_count")
        count = int(raw_count) if isinstance(raw_count, (int, float)) else None
        if count is None and records:
            count = len(records)
        return records, count, _compact(data.get("summary"), 220)
    return [], None, ""


def _read_status(item: Mapping[str, Any], record: Mapping[str, Any], body: str) -> str:
    raw = _text(
        item.get("read_status")
        or item.get("retrieval_status")
        or record.get("read_status")
        or record.get("retrieval_status")
    ).upper().replace("-", "_")
    if raw in {"PARTIAL", "PARTIALLY_READ", "PARTIAL_SUCCESS"}:
        return "PARTIAL"
    if raw in {"METADATA_ONLY", "METADATA", "TITLE_ONLY"} or not body:
        return "METADATA_ONLY"
    return "FULL"


def _detail_sources(tool_results: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int, int]:
    sources: list[dict[str, Any]] = []
    failed = 0
    selected = 0
    seen: set[tuple[str, str]] = set()
    for item in tool_results:
        if not _is_detail(item):
            continue
        if not _is_success(item):
            failed += 1
            selected += 1
            continue
        records = _records(_payload(item))
        if not records:
            selected += 1
            continue
        for record in records[:1]:
            selected += 1
            resource_id = _resource_id(record)
            key = (resource_id, _title(record))
            if key in seen:
                continue
            seen.add(key)
            body = _body(record)
            read_status = _read_status(item, record, body)
            sources.append({
                "ref": f"source-{len(sources) + 1}",
                "resource_id": resource_id,
                "title": _title(record),
                "summary": _summary(record),
                "body": body[:_MAX_EVIDENCE_CHARS],
                "read_status": read_status,
            })
            if len(sources) >= _MAX_SOURCES:
                return sources, failed, selected
    return sources, failed, selected


def _ordered_search_items(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        resource_id = _resource_id(record)
        title = _title(record)
        key = (resource_id, title)
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "id": resource_id or f"result-{index}",
            "resource_id": resource_id,
            "title": title,
            "excerpt": _summary(record),
            "summary": _summary(record),
            "href": f"/post/{resource_id}" if resource_id else None,
        })
        if len(result) >= _MAX_SEARCH_ITEMS:
            break
    return result


def _source_items(sources: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "resource_id": _text(source.get("resource_id")) or None,
            "title": _text(source.get("title")) or "未命名内容",
            "excerpt": _clean_excerpt(source.get("summary"), 260) or None,
            "summary": _clean_excerpt(source.get("summary"), 260) or None,
            "href": (
                f"/post/{source['resource_id']}"
                if _text(source.get("resource_id"))
                else None
            ),
            "read_status": _text(source.get("read_status")) or "FULL",
            "source_refs": (
                [_text(source.get("ref"))]
                if _text(source.get("ref"))
                else []
            ),
        }
        for source in sources
    ]


def _response_payload(response: Any) -> Any:
    message = response.choices[0].message
    parsed = getattr(message, "parsed", None)
    if parsed is not None:
        return parsed.model_dump(mode="python") if hasattr(parsed, "model_dump") else parsed
    content = getattr(message, "content", None)
    if isinstance(content, Mapping):
        return dict(content)
    if isinstance(content, str) and content.strip():
        return json.loads(content)
    raise ValueError("structured synthesis response is empty")


async def _synthesize(
    *,
    request: str,
    sources: Sequence[Mapping[str, Any]],
    llm: Any | None,
    model: str,
) -> _SynthesisDraft | None:
    if llm is None or not sources:
        return None

    evidence = [
        {
            "source_ref": source["ref"],
            "title": source["title"],
            "summary": source["summary"],
            "content": source["body"],
        }
        for source in sources
    ]
    schema = _SynthesisDraft.model_json_schema()
    system = """You are GreenBook's user-facing evidence synthesis stage.

Use only the supplied retrieved source excerpts. Return one JSON object that
matches the schema. Write in the same language as the user's request. Do not
mention tools, agents, execution, reflection, goals, prompts, or task status.
Do not add facts that are absent from the evidence. A common pattern must be
supported by at least two distinct source_ref values; otherwise omit it.
Use differences only when the sources genuinely diverge or the user asks for
a comparison, and require at least two distinct source_ref values for every
difference. Keep the explanation concise and natural for the user. Put all
source_ref values only in the structured source_refs arrays; never write
source-1, source-2, evidence IDs, artifact IDs, UUIDs, or other internal
references in title, explanation, intro, or conclusion. Never infer missing
details from a title or a partial source. Do not turn an uncertain guess into
a concrete fact with words such as may, might, possibly, probably, or 可能.
Omit any point that cannot be directly supported by the supplied evidence.
"""
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": json.dumps(
                {"user_request": request, "sources": evidence},
                ensure_ascii=False,
            ),
        },
    ]
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "greenbook_user_facing_synthesis",
                "strict": True,
                "schema": schema,
            },
        },
        "temperature": 0.0,
        "max_tokens": 2400,
        **structured_provider_options(llm, model),
    }
    try:
        try:
            response = await llm.chat.completions.create(**kwargs)
        except Exception as exc:
            if "response_format" not in str(exc).lower() and "json_schema" not in str(exc).lower():
                raise
            fallback = dict(kwargs)
            fallback["response_format"] = {"type": "json_object"}
            fallback["messages"] = add_json_schema_instruction(messages, schema)
            response = await llm.chat.completions.create(**fallback)
        if not has_structured_payload(response):
            fallback = dict(kwargs)
            fallback["response_format"] = {"type": "json_object"}
            fallback["messages"] = add_json_schema_instruction(messages, schema)
            response = await llm.chat.completions.create(**fallback)
        return _SynthesisDraft.model_validate(_response_payload(response))
    except (ValidationError, ValueError, TypeError, json.JSONDecodeError):
        logger.warning("User-facing synthesis returned an invalid structured payload", exc_info=True)
    except Exception:
        logger.warning("User-facing synthesis call failed", exc_info=True)
    return None


def _grounded_points(
    points: Sequence[_SynthesisPoint],
    known_refs: set[str],
    *,
    require_multiple: bool,
) -> list[dict[str, Any]]:
    grounded: list[dict[str, Any]] = []
    for point in points:
        refs = list(dict.fromkeys(ref for ref in point.source_refs if ref in known_refs))
        if require_multiple and len(refs) < 2:
            continue
        if not refs or not _text(point.title) or not _text(point.explanation):
            continue
        if _INTERNAL_REFERENCE.search(f"{point.title} {point.explanation}"):
            continue
        if _UNVERIFIED_HEDGE.search(f"{point.title} {point.explanation}"):
            continue
        grounded.append({
            "title": _compact(point.title, 120),
            "explanation": _clean_excerpt(point.explanation, 420),
            "source_refs": refs,
        })
    return grounded


def _safe_conclusion(value: Any) -> str:
    conclusion = _clean_excerpt(value, 600)
    if (
        not conclusion
        or _INTERNAL_REFERENCE.search(conclusion)
        or _UNVERIFIED_HEDGE.search(conclusion)
    ):
        return ""
    return conclusion


def _evidence_note(
    copy: Mapping[str, str],
    *,
    total: int | None,
    read_count: int,
    failed_count: int,
    incomplete_count: int = 0,
) -> str | None:
    if not total and not read_count:
        return copy["no_results"]
    if read_count < 2:
        if failed_count or incomplete_count:
            return f"{copy['insufficient']} {copy['partial']}"
        return copy["insufficient"]
    if failed_count or incomplete_count:
        return copy["partial"]
    return None


def _interaction_message(interaction: Mapping[str, Any]) -> str:
    kind = _text(interaction.get("kind")).upper()
    if kind == "SYNTHESIS_RESULT":
        synthesis = _mapping(interaction.get("synthesis"))
        conclusion = _text(synthesis.get("conclusion"))
        if conclusion:
            return conclusion
        # No reliable common-pattern conclusion (e.g. < 2 full sources): surface
        # the concrete evidence insufficiency instead of a bare count string.
        note = _text(synthesis.get("evidence_note"))
        if note:
            return note
        return _text(synthesis.get("intro") or synthesis.get("title"))
    result = _mapping(interaction.get("result"))
    return _text(result.get("title") or result.get("summary"))


async def build_retrieval_interaction(
    *,
    request: str,
    tool_results: Sequence[Mapping[str, Any]],
    synthesis_requested: bool,
    llm: Any | None = None,
    model: str = "",
) -> tuple[dict[str, Any] | None, str]:
    """Return ``(interaction, safe_message)`` for a read-only turn.

    Pure search stays ``QUERY_RESULT``.  A read set plus a synthesis-capable
    turn becomes ``SYNTHESIS_RESULT``.  The returned message is only a short
    business summary; reflection output is intentionally never consulted.
    """

    normalized_results = [dict(item) for item in tool_results if isinstance(item, Mapping)]
    search_records, total_matched, search_summary = _search_snapshot(normalized_results)
    sources, failed_count, selected_count = _detail_sources(normalized_results)
    readable_sources = [
        source for source in sources
        if _text(source.get("read_status")) in _READABLE_STATUSES
        and _text(source.get("body"))
    ]
    has_search = any(_is_search(item) for item in normalized_results)
    has_detail = any(_is_detail(item) for item in normalized_results)
    if not has_search and not has_detail:
        return None, ""

    copy = _language_copy(request)
    should_synthesize = synthesis_requested or len(readable_sources) >= 2
    if not should_synthesize:
        count = total_matched if total_matched is not None else len(search_records)
        if count:
            title = f"{copy['found']} {count} {copy['related']}"
        elif search_records:
            title = f"{copy['found']} {copy['related']}"
        else:
            title = copy["no_results"]
        interaction = {
            "kind": "QUERY_RESULT",
            "result": {
                "type": "SEARCH_RESULTS",
                "status": "SUCCESS",
                "language": "zh" if _language_is_chinese(request) else "en",
                "title": title,
                "summary": search_summary or None,
                "search": {
                    "count": count or None,
                    "items": _ordered_search_items(search_records),
                },
            },
        }
        return interaction, _interaction_message(interaction)

    total = (
        total_matched
        if total_matched is not None
        else len(search_records) if has_search else len(sources)
    )
    single_source_summary = (
        synthesis_requested
        and not has_search
        and len(sources) == 1
        and len(readable_sources) == 1
        and failed_count == 0
    )
    draft = await _synthesize(
        request=request,
        sources=readable_sources,
        llm=llm,
        model=model,
    )
    known_refs = {str(source["ref"]) for source in readable_sources}
    common_patterns = _grounded_points(
        draft.common_patterns if draft else [],
        known_refs,
        require_multiple=True,
    )
    differences = _grounded_points(
        draft.differences if draft else [],
        known_refs,
        require_multiple=True,
    )
    note = None if single_source_summary else _evidence_note(
        copy,
        total=total,
        read_count=len(readable_sources),
        failed_count=failed_count,
        incomplete_count=sum(
            1 for source in sources
            if _text(source.get("read_status")) != "FULL"
        ),
    )
    if not common_patterns and not note and not single_source_summary:
        note = copy["no_common"]
    conclusion = ""
    if draft and (common_patterns or differences or single_source_summary):
        conclusion = _safe_conclusion(draft.conclusion)
    if single_source_summary and not conclusion:
        conclusion = _clean_excerpt(readable_sources[0].get("body"), 600)
    if _language_is_chinese(request):
        intro = (
            f"{copy['found']} {total} {copy['related']}，选择了 {selected_count} 篇，"
            f"成功读取 {len(readable_sources)} 篇。"
            if selected_count and selected_count != len(readable_sources)
            else f"{copy['found']} {total} {copy['related']}，{copy['read']} {len(readable_sources)} 篇。"
        )
    else:
        intro = (
            f"{copy['found']} {total} {copy['related']}; selected {selected_count}, "
            f"successfully read {len(readable_sources)}."
            if selected_count and selected_count != len(readable_sources)
            else f"{copy['found']} {total} {copy['related']}; {copy['read'].lower()} {len(readable_sources)}."
        )
    if failed_count:
        intro = f"{intro} {copy['partial']}"
    interaction = {
        "kind": "SYNTHESIS_RESULT",
        "status": (
            "PARTIAL_SUCCESS"
            if failed_count or any(
                _text(source.get("read_status")) != "FULL" for source in sources
            )
            else "SUCCESS"
        ),
        "synthesis": {
            "title": copy["title"],
            "language": "zh" if _language_is_chinese(request) else "en",
            "intro": intro,
            "total_matched": total or None,
            "selected_count": selected_count or None,
            "read_count": len(readable_sources),
            "failed_count": failed_count,
            "sources": _source_items(sources),
            "common_patterns": common_patterns,
            "differences": differences,
            "conclusion": conclusion,
            "evidence_note": note,
        },
    }
    return interaction, _interaction_message(interaction)


__all__ = ["build_retrieval_interaction"]
