"""Candidate retrieval and deterministic reranking for long-term memory."""

from __future__ import annotations

import inspect
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .models import MemoryQuery, MemoryRecord, MemoryStatus, MemoryType
from .relevance import MemoryRelevanceGate, lexical_relevance

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+")
_STOPWORDS = frozenset({
    "a",
    "an",
    "and",
    "are",
    "at",
    "be",
    "by",
    "for",
    "from",
    "give",
    "how",
    "in",
    "is",
    "it",
    "me",
    "of",
    "on",
    "or",
    "please",
    "the",
    "to",
    "use",
    "what",
    "when",
    "with",
    "you",
})


class MemoryRetriever:
    """Retrieve, rerank, filter, and audit memory usage.

    No fake embeddings are generated.  When a semantic provider is added it
    can be injected as ``candidate_provider``; the deterministic repository
    search remains the safe fallback.
    """

    def __init__(
        self,
        repository: Any,
        *,
        candidate_provider: Any | None = None,
        relevance_threshold: float = 0.1,
        confidence_threshold: float = 0.0,
        memory_types: Iterable[MemoryType | str] | None = None,
        status: MemoryStatus | None = None,
        include_legacy_episodic: bool = True,
        require_tenant_scope: bool = False,
        semantic_contract: str | None = None,
        procedural_contract: str | None = None,
        include_preference_alias: bool = True,
    ) -> None:
        self._repository = repository
        self._candidate_provider = candidate_provider
        self._memory_types = _normalise_memory_types(memory_types)
        self._status = status
        self._include_legacy_episodic = bool(include_legacy_episodic)
        self._require_tenant_scope = bool(require_tenant_scope)
        self._semantic_contract = str(semantic_contract or "").strip()
        self._procedural_contract = str(
            procedural_contract
            or (
                "PROCEDURAL_V1"
                if MemoryType.PROCEDURAL in (self._memory_types or ())
                else ""
            )
        ).strip()
        self._include_preference_alias = bool(include_preference_alias)
        self._relevance_gate = MemoryRelevanceGate(
            relevance_threshold=relevance_threshold,
            confidence_threshold=confidence_threshold,
        )

    async def retrieve(
        self,
        *,
        user_id: str,
        tenant_id: str = "",
        conversation_id: str = "",
        task_id: str = "",
        command: Any | None = None,
        goal: Any | None = None,
        context: Any | None = None,
        target_query: str = "",
        limit: int = 8,
        run_id: str = "",
        touch: bool = True,
    ) -> list[MemoryRecord]:
        from greenbook_agent_core.observability.run_metrics import (
            record_memory_retrieval,
            record_stage,
        )

        record_stage("memory_retrieval_start", run_id=run_id)
        if self._require_tenant_scope and not str(tenant_id or "").strip():
            record_stage("memory_candidates_ready", run_id=run_id)
            record_stage("memory_ranking_ready", run_id=run_id)
            record_stage("memory_touch_start", run_id=run_id)
            record_stage("memory_touch_ready", run_id=run_id)
            record_stage("memory_retrieval_ready", run_id=run_id)
            record_memory_retrieval(
                source="repository",
                candidate_count=0,
                selected_count=0,
                memory_types=[],
                run_id=run_id,
            )
            return []
        terms = _query_terms(command, goal, context)
        terms.extend(_tokenize(target_query))
        terms = _meaningful_terms(terms)
        provider = self._candidate_provider
        if provider is not None and callable(getattr(provider, "retrieve", None)):
            try:
                candidates = provider.retrieve(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    query_terms=terms,
                    limit=max(limit * 5, limit),
                )
            except TypeError:
                # Candidate providers from the pre-tenant contract remain
                # usable, while the canonical retriever still applies the
                # scope/type/legacy filters below.
                candidates = provider.retrieve(
                    user_id=user_id,
                    conversation_id=conversation_id,
                    task_id=task_id,
                    query_terms=terms,
                    limit=max(limit * 5, limit),
                )
        else:
            candidates = self._search_repository(
                user_id=user_id,
                tenant_id=tenant_id,
                terms=terms,
                limit=max(limit * 5, limit),
            )
        candidates = await candidates if inspect.isawaitable(candidates) else candidates
        record_stage("memory_candidates_ready", run_id=run_id)
        values = [
            item if isinstance(item, MemoryRecord) else MemoryRecord.model_validate(item)
            for item in (candidates or ())
        ]
        values = [
            item
            for item in values
            if self._allowed_candidate(item, user_id=user_id, tenant_id=tenant_id)
        ]
        if _procedure_override_requested(command, goal, target_query):
            values = [
                item
                for item in values
                if item.memory_type != MemoryType.PROCEDURAL
            ]
        ranked = sorted(
            values,
            key=lambda item: _score(item, terms, conversation_id, task_id),
            reverse=True,
        )
        relevance = self._relevance_gate.evaluate(
            ranked,
            score=lambda item: _relevance_score(
                item,
                terms,
                conversation_id,
                task_id,
            ),
            limit=limit,
        )
        selected = list(relevance.selected)
        record_stage("memory_ranking_ready", run_id=run_id)
        record_stage("memory_touch_start", run_id=run_id)
        touched: list[MemoryRecord] = list(selected)
        if touch:
            touched = []
            for item in selected:
                touch_fn = getattr(self._repository, "touch", None)
                if callable(touch_fn):
                    try:
                        value = touch_fn(
                            item.memory_id,
                            user_id=user_id,
                            tenant_id=tenant_id if self._require_tenant_scope else None,
                        )
                    except TypeError:
                        value = touch_fn(item.memory_id)
                    value = await value if inspect.isawaitable(value) else value
                    touched.append(value or item)
                else:
                    touched.append(item)
        record_stage("memory_touch_ready", run_id=run_id)
        record_stage("memory_retrieval_ready", run_id=run_id)
        record_memory_retrieval(
            source="candidate_provider" if provider is not None else "repository",
            candidate_count=len(values),
            selected_count=len(touched),
            memory_types=[str(item.memory_type) for item in touched],
            run_id=run_id,
        )
        return touched

    async def _search_repository(
        self,
        *,
        user_id: str,
        tenant_id: str,
        terms: list[str],
        limit: int,
    ) -> list[MemoryRecord]:
        """Search all configured types through one repository and one gate.

        Episodic V1 uses the existing ``agent_memories`` table but carries an
        explicit contract marker.  A per-type query keeps legacy EPISODIC rows
        out of the canonical path without introducing a second retriever or
        relevance policy.  The non-strict default retains compatibility with
        old unscoped in-memory callers; production passes ``require_tenant_scope``.
        """

        queries = self._queries(
            user_id=user_id,
            tenant_id=tenant_id,
            terms=terms,
            limit=limit,
        )
        values: list[MemoryRecord] = []
        for query in queries:
            found = self._repository.search(query)
            found = await found if inspect.isawaitable(found) else found
            values.extend(
                item if isinstance(item, MemoryRecord) else MemoryRecord.model_validate(item)
                for item in (found or ())
            )
        return _dedupe(values)

    def _queries(
        self,
        *,
        user_id: str,
        tenant_id: str,
        terms: list[str],
        limit: int,
    ) -> list[MemoryQuery]:
        memory_types = self._memory_types
        # The legacy-compatible default historically searched the empty tenant
        # value.  Strict production composition uses the authenticated tenant.
        tenants = [tenant_id] if self._require_tenant_scope else list(
            dict.fromkeys(value for value in ("", tenant_id) if value is not None)
        )
        if not tenants:
            tenants = [""]
        queries: list[MemoryQuery] = []
        for query_tenant in tenants:
            if memory_types is not None:
                for memory_type in memory_types:
                    if memory_type == MemoryType.SEMANTIC and self._semantic_contract:
                        # PREFERENCE and SEMANTIC intentionally share the
                        # persisted enum value. Search both the existing
                        # Preference projection and the explicit Semantic V1
                        # contract, then classify them in the one gate path.
                        metadata_variants = (
                            ({},) if self._include_preference_alias else ()
                        ) + ((
                            {
                                "memory_contract": self._semantic_contract,
                                "memory_role": "stable_fact",
                            },
                        ))
                        for metadata_filters in metadata_variants:
                            queries.append(MemoryQuery(
                                user_id=user_id,
                                tenant_id=query_tenant,
                                type=memory_type,
                                status=self._status,
                                metadata_filters=metadata_filters,
                                conversation_id=None,
                                task_id=None,
                                keywords=terms[:12],
                                limit=limit,
                                sort_by="created_at",
                            ))
                        continue
                    metadata_filters = {}
                    if (
                        memory_type == MemoryType.EPISODIC
                        and not self._include_legacy_episodic
                    ):
                        metadata_filters = {
                            "memory_contract": "EPISODIC_V1",
                        }
                    if (
                        memory_type == MemoryType.PROCEDURAL
                        and self._procedural_contract
                    ):
                        metadata_filters = {
                            "memory_contract": self._procedural_contract,
                            "memory_role": "relevant_procedure",
                        }
                    queries.append(MemoryQuery(
                        user_id=user_id,
                        tenant_id=query_tenant,
                        type=memory_type,
                        status=self._status,
                        metadata_filters=metadata_filters,
                        conversation_id=None,
                        task_id=None,
                        keywords=terms[:12],
                        limit=limit,
                        sort_by="created_at",
                    ))
                continue
            queries.append(MemoryQuery(
                user_id=user_id,
                tenant_id=query_tenant,
                status=self._status,
                conversation_id=None,
                task_id=None,
                keywords=terms[:12],
                limit=limit,
                sort_by="created_at",
            ))
        return queries

    def _allowed_candidate(
        self,
        item: MemoryRecord,
        *,
        user_id: str,
        tenant_id: str,
    ) -> bool:
        if item.user_id != user_id:
            return False
        if self._require_tenant_scope and item.tenant_id != tenant_id:
            return False
        if not self._require_tenant_scope and tenant_id and item.tenant_id not in {"", tenant_id}:
            return False
        if self._memory_types is not None and item.memory_type not in self._memory_types:
            return False
        if self._status is not None and item.status != self._status:
            return False
        if self._semantic_contract and item.memory_type == MemoryType.SEMANTIC:
            preference_like = bool(
                item.metadata.get("preference_type")
                and item.metadata.get("value")
            )
            semantic_like = (
                item.metadata.get("memory_contract") == self._semantic_contract
                and item.metadata.get("memory_role") == "stable_fact"
            )
            if self._include_preference_alias:
                allowed = preference_like or semantic_like
            else:
                allowed = semantic_like
            if not allowed:
                return False
        if (
            self._procedural_contract
            and item.memory_type == MemoryType.PROCEDURAL
            and (
                item.metadata.get("memory_contract") != self._procedural_contract
                or item.metadata.get("memory_role") != "relevant_procedure"
            )
        ):
            return False
        return not (
            item.memory_type == MemoryType.EPISODIC
            and not self._include_legacy_episodic
            and item.metadata.get("memory_contract") != "EPISODIC_V1"
        )


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
        terms.extend(_tokenize(value))
    return _meaningful_terms(terms)


def _procedure_override_requested(
    command: Any,
    goal: Any,
    target_query: str,
) -> bool:
    """Keep a current explicit exception from inheriting old soft guidance."""

    values: list[str] = [str(target_query or "")]
    for item in (command, goal):
        payload = item.model_dump(mode="json") if hasattr(item, "model_dump") else item
        if isinstance(payload, Mapping):
            values.extend(
                str(payload.get(key, ""))
                for key in ("raw_input", "objective", "description")
            )
        elif item:
            values.append(str(item))
    text = " ".join(values).casefold()
    return any(marker in text for marker in (
        "\u4e0d\u7528\u5927\u7eb2",
        "\u4e0d\u7528\u5148\u5217\u5927\u7eb2",
        "\u4e0d\u8981\u5927\u7eb2",
        "\u4e0d\u8981\u5148\u5217\u5927\u7eb2",
        "\u65e0\u9700\u5927\u7eb2",
        "\u4e0d\u9700\u8981\u5927\u7eb2",
        "\u4e0d\u9700\u8981\u5148\u5217\u5927\u7eb2",
        "\u8df3\u8fc7\u5927\u7eb2",
        "\u4e0d\u5217\u5927\u7eb2",
        "without an outline",
        "skip the outline",
        "no outline",
        "do not use an outline",
        "don't use an outline",
        "write directly",
        "directly write",
    ))


def _meaningful_terms(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(
        term.casefold()
        for term in values
        if len(term) > 1 and term.casefold() not in _STOPWORDS
    ))


def _tokenize(value: Any) -> list[str]:
    terms: list[str] = []
    for token in _WORD_RE.findall(str(value or "").casefold()):
        if token and all("\u4e00" <= char <= "\u9fff" for char in token):
            if len(token) > 1:
                terms.append(token)
                terms.extend(token[index:index + 2] for index in range(len(token) - 1))
        else:
            terms.append(token)
    return terms


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


def _relevance_score(
    item: MemoryRecord,
    terms: list[str],
    conversation_id: str,
    task_id: str,
) -> float:
    relation = 0.0
    if conversation_id and item.conversation_id == conversation_id:
        relation = max(relation, 1.0)
    if task_id and item.task_id == task_id:
        relation = max(relation, 1.0)
    lexical = lexical_relevance(
        " ".join([item.content, str(item.metadata)]),
        terms,
    )
    return max(lexical, relation)


def _recency(value: str) -> float:
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(value)).total_seconds()
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, 1.0 - age / (86400 * 30))


def _normalise_memory_types(
    values: Iterable[MemoryType | str] | None,
) -> tuple[MemoryType, ...] | None:
    if values is None:
        return None
    result: list[MemoryType] = []
    for value in values:
        if isinstance(value, MemoryType):
            memory_type = value
        else:
            raw = str(value or "").strip().upper()
            if raw == "PREFERENCE":
                raw = MemoryType.PREFERENCE.value
            try:
                memory_type = MemoryType(raw)
            except ValueError:
                continue
        if memory_type not in result:
            result.append(memory_type)
    return tuple(result)


def _dedupe(values: Iterable[MemoryRecord]) -> list[MemoryRecord]:
    result: list[MemoryRecord] = []
    seen: set[str] = set()
    for value in values:
        if value.memory_id in seen:
            continue
        seen.add(value.memory_id)
        result.append(value)
    return result


__all__ = ["MemoryRetriever"]
