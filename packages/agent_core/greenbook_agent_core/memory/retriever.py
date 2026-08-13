"""Candidate retrieval and deterministic reranking for long-term memory."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .models import MemoryQuery, MemoryRecord

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class MemoryRetriever:
    """Retrieve, rerank, filter, and audit memory usage.

    No fake embeddings are generated.  When a semantic provider is added it
    can be injected as ``candidate_provider``; the deterministic repository
    search remains the safe fallback.
    """

    def __init__(self, repository: Any, *, candidate_provider: Any | None = None) -> None:
        self._repository = repository
        self._candidate_provider = candidate_provider

    async def retrieve(
        self,
        *,
        user_id: str,
        conversation_id: str = "",
        task_id: str = "",
        command: Any | None = None,
        goal: Any | None = None,
        context: Any | None = None,
        target_query: str = "",
        limit: int = 8,
    ) -> list[MemoryRecord]:
        terms = _query_terms(command, goal, context)
        terms.extend(_WORD_RE.findall(target_query.casefold()))
        terms = list(dict.fromkeys(term for term in terms if len(term) > 1))
        query = MemoryQuery(
            user_id=user_id,
            # Long-term memory is intentionally cross-conversation.  Relation
            # fields are reranking evidence, not hard filters.
            conversation_id=None,
            task_id=None,
            keywords=terms[:12],
            limit=max(limit * 5, limit),
            sort_by="created_at",
        )
        provider = self._candidate_provider
        if provider is not None and callable(getattr(provider, "retrieve", None)):
            candidates = provider.retrieve(
                user_id=user_id,
                conversation_id=conversation_id,
                task_id=task_id,
                query_terms=terms,
                limit=max(limit * 5, limit),
            )
        else:
            candidates = self._repository.search(query)
        candidates = await candidates if inspect.isawaitable(candidates) else candidates
        values = [item if isinstance(item, MemoryRecord) else MemoryRecord.model_validate(item) for item in candidates]
        ranked = sorted(
            values,
            key=lambda item: _score(item, terms, conversation_id, task_id),
            reverse=True,
        )
        selected = [item for item in ranked if _score(item, terms, conversation_id, task_id) > 0][:limit]
        if not selected and not terms:
            selected = ranked[:limit]
        touched: list[MemoryRecord] = []
        for item in selected:
            touch = getattr(self._repository, "touch", None)
            if callable(touch):
                value = touch(item.memory_id)
                value = await value if inspect.isawaitable(value) else value
                touched.append(value or item)
            else:
                touched.append(item)
        return touched


def _query_terms(command: Any, goal: Any, context: Any) -> list[str]:
    values: list[str] = []
    for item in (command, goal):
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        if isinstance(payload, Mapping):
            values.extend(str(payload.get(key, "")) for key in ("objective", "description", "goal_type", "raw_input"))
            values.extend(str(value) for value in (payload.get("parameters") or {}).values())
        elif item:
            values.append(str(item))
    if context is not None:
        payload = context.decision_payload() if callable(getattr(context, "decision_payload", None)) else context
        if isinstance(payload, Mapping):
            values.extend(str(item.get("goal", "")) for item in payload.get("active_tasks", []) if isinstance(item, Mapping))
    terms: list[str] = []
    for value in values:
        terms.extend(_WORD_RE.findall(value.casefold()))
    return list(dict.fromkeys(term for term in terms if len(term) > 1))


def _score(item: MemoryRecord, terms: list[str], conversation_id: str, task_id: str) -> float:
    haystack = " ".join([item.content, str(item.metadata)]).casefold()
    overlap = sum(1 for term in terms if term in haystack)
    relation = 0.0
    if conversation_id and item.conversation_id == conversation_id:
        relation += 0.35
    if task_id and item.task_id == task_id:
        relation += 0.5
    recency = _recency(item.updated_at)
    return overlap * 1.0 + relation + item.importance * 0.25 + item.confidence * 0.2 + recency * 0.1


def _recency(value: str) -> float:
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, 1.0 - age / (86400 * 30))


__all__ = ["MemoryRetriever"]
