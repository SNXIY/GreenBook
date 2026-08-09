from __future__ import annotations

import re
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any


SearchCallable = Callable[[str, int], Awaitable[Sequence[dict[str, Any]]]]

_WHITESPACE = re.compile(r"\s+")
_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9+#._-]{1,31}")
_LEADING_PATTERNS = (
    re.compile(r"^(?:请|麻烦|劳驾|请你|可以|能否|能不能)\s*"),
    re.compile(r"^(?:帮我|给我|替我|为我)\s*"),
    re.compile(r"^(?:在(?:这个|本)?社区(?:里|中)?\s*)"),
    re.compile(r"^(?:检索|搜索|查找|查询|搜(?:索)?|找(?:到|出)?|看看)\s*"),
    re.compile(r"^(?:一下|一些|几篇|几条|几份|若干)\s*"),
    re.compile(r"^(?:有关|关于)\s*"),
    re.compile(r"^(?:如何|怎么|怎样)\s*(?:学好|学习|掌握|入门|理解)?\s*"),
)
_TRAILING_PATTERNS = (
    re.compile(r"(?:相关)?(?:的)?(?:帖子|文章|内容|资料|教程)\s*$"),
    re.compile(r"(?:给我)?(?:参考|看看|学习)\s*$"),
)


@dataclass(frozen=True, slots=True)
class SearchQueryPlan:
    original_query: str
    candidates: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchRetrievalResult:
    original_query: str
    matched_query: str | None
    attempted_queries: tuple[str, ...]
    results: tuple[dict[str, Any], ...]


def build_search_query_plan(query: str, *, max_candidates: int = 5) -> SearchQueryPlan:
    """Build bounded lexical fallbacks without another model round-trip.

    The planner keeps the model-provided query first. It only broadens retrieval
    after an empty result, removing conversational search framing while retaining
    the user's topic. ASCII technical terms are also useful fallbacks for mixed
    Chinese/English requests such as ``如何学好 Agent``.
    """

    normalized = _normalize(query)
    if not normalized:
        return SearchQueryPlan(original_query="", candidates=())

    candidates: list[str] = []
    _append_unique(candidates, normalized, max_candidates)

    topic = _strip_search_framing(normalized)
    _append_unique(candidates, topic, max_candidates)

    ascii_terms = _ASCII_TERM.findall(topic or normalized)
    if len(ascii_terms) > 1:
        _append_unique(candidates, " ".join(ascii_terms), max_candidates)
    for term in ascii_terms:
        _append_unique(candidates, term, max_candidates)

    return SearchQueryPlan(
        original_query=normalized,
        candidates=tuple(candidates[:max_candidates]),
    )


async def search_with_fallback(
    query: str,
    limit: int,
    search: SearchCallable,
    *,
    max_candidates: int = 5,
) -> SearchRetrievalResult:
    """Try the precise query first and broaden only when it returns no rows."""

    plan = build_search_query_plan(query, max_candidates=max_candidates)
    attempted: list[str] = []
    bounded_limit = max(1, min(int(limit), 10))
    for candidate in plan.candidates:
        attempted.append(candidate)
        rows = list(await search(candidate, bounded_limit))
        if not rows:
            continue
        deduplicated: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in rows:
            post_id = str(row.get("id") or "")
            if not post_id or post_id in seen_ids:
                continue
            seen_ids.add(post_id)
            deduplicated.append(dict(row))
            if len(deduplicated) >= bounded_limit:
                break
        if deduplicated:
            return SearchRetrievalResult(
                original_query=plan.original_query,
                matched_query=candidate,
                attempted_queries=tuple(attempted),
                results=tuple(deduplicated),
            )

    return SearchRetrievalResult(
        original_query=plan.original_query,
        matched_query=None,
        attempted_queries=tuple(attempted),
        results=(),
    )


def _normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return _WHITESPACE.sub(" ", normalized).strip(" \t\r\n，。！？；：,.!?;:")


def _strip_search_framing(query: str) -> str:
    topic = query
    changed = True
    while changed and topic:
        changed = False
        for pattern in _LEADING_PATTERNS:
            updated = pattern.sub("", topic, count=1).strip()
            if updated != topic:
                topic = updated
                changed = True
    changed = True
    while changed and topic:
        changed = False
        for pattern in _TRAILING_PATTERNS:
            updated = pattern.sub("", topic, count=1).strip()
            if updated != topic:
                topic = updated
                changed = True
    return _normalize(topic)


def _append_unique(values: list[str], candidate: str, limit: int) -> None:
    normalized = _normalize(candidate)
    if not normalized or len(values) >= limit:
        return
    keys = {value.casefold() for value in values}
    if normalized.casefold() not in keys:
        values.append(normalized)
