"""Tenant-scoped retrieval for the Preference Memory vertical slice."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from .models import MemoryQuery, MemoryRecord, MemoryStatus, MemoryType

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_MAX_PREFERENCE_RECALL = 5


class PreferenceRetriever:
    """Retrieve bounded, active preferences for one authenticated scope."""

    def __init__(self, repository: Any, *, default_limit: int = 5) -> None:
        self._repository = repository
        self._default_limit = max(1, min(int(default_limit), _MAX_PREFERENCE_RECALL))

    async def retrieve(
        self,
        *,
        user_id: str,
        tenant_id: str,
        query: str = "",
        target_query: str = "",
        command: Any | None = None,
        goal: Any | None = None,
        limit: int | None = None,
        touch: bool = False,
        **_: Any,
    ) -> list[MemoryRecord]:
        # A missing tenant is a fail-closed condition for the new durable
        # preference path. Legacy unscoped records must not leak into a new
        # authenticated scope.
        if not str(user_id or "").strip() or not str(tenant_id or "").strip():
            return []
        selected_limit = self._limit(limit)
        terms = self._terms(query, target_query, command, goal)
        search = getattr(self._repository, "search", None)
        if not callable(search):
            return []
        candidates = search(MemoryQuery(
            user_id=user_id,
            tenant_id=tenant_id,
            type=MemoryType.PREFERENCE,
            status=MemoryStatus.ACTIVE,
            # Relevance is ranked here so an article query can still receive
            # a bounded profile preference whose wording does not share a
            # token with the current request.
            limit=max(selected_limit * 8, selected_limit),
            sort_by="created_at",
        ))
        candidates = await candidates if inspect.isawaitable(candidates) else candidates
        values = [
            item if isinstance(item, MemoryRecord) else MemoryRecord.model_validate(item)
            for item in (candidates or ())
            if item is not None
        ]
        ranked = sorted(
            values,
            key=lambda item: self._score(item, terms),
            reverse=True,
        )[:selected_limit]
        if not touch:
            return ranked

        touched: list[MemoryRecord] = []
        for item in ranked:
            touch_fn = getattr(self._repository, "touch", None)
            if not callable(touch_fn):
                touched.append(item)
                continue
            try:
                value = touch_fn(
                    item.memory_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            except TypeError:
                value = touch_fn(item.memory_id)
            value = await value if inspect.isawaitable(value) else value
            touched.append(value or item)
        return touched

    def _limit(self, value: int | None) -> int:
        return max(
            1,
            min(
                _MAX_PREFERENCE_RECALL,
                int(value) if value is not None else self._default_limit,
            ),
        )

    @staticmethod
    def _terms(*values: Any) -> list[str]:
        terms: list[str] = []
        for value in values:
            if value is None:
                continue
            payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            if isinstance(payload, Mapping):
                text = " ".join(str(item) for item in payload.values())
            else:
                text = str(payload)
            terms.extend(_WORD_RE.findall(text.casefold()))
        return list(dict.fromkeys(term for term in terms if len(term) > 1))

    @staticmethod
    def _score(item: MemoryRecord, terms: list[str]) -> float:
        haystack = " ".join([
            item.content,
            str(item.metadata.get("preference_type", "")),
            str(item.metadata.get("value", "")),
        ]).casefold()
        overlap = sum(1 for term in terms if term in haystack)
        recency = 0.0
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(item.updated_at)).total_seconds()
            recency = max(0.0, 1.0 - age / (86400 * 30))
        except (TypeError, ValueError):
            pass
        return overlap * 2.0 + item.confidence * 0.8 + item.importance * 0.3 + recency * 0.1


__all__ = ["PreferenceRetriever"]
