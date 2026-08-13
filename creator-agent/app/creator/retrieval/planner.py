from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.creator.domain.models import CreatorTaskKind
from app.creator.privacy import CreatorPrivacySanitizer
from app.creator.retrieval.models import (
    CreatorEvidenceGrade,
    CreatorRetrievalConfig,
    CreatorRetrievalFilters,
    CreatorRetrievalPlan,
    CreatorRetrievalRequest,
    RetrievalChannel,
    RetrievalIntent,
)

_CHANNEL_ALIASES = {
    "VECTOR": RetrievalChannel.QDRANT,
    "SEMANTIC": RetrievalChannel.QDRANT,
    "QDRANT": RetrievalChannel.QDRANT,
    "DATABASE": RetrievalChannel.SQL,
    "POSTGRES": RetrievalChannel.SQL,
    "POSTGRESQL": RetrievalChannel.SQL,
    "SQL": RetrievalChannel.SQL,
}


class CreatorRetrievalPlanner:
    """Builds bounded tool plans from task intent and authorized source scope."""

    def __init__(self, config: CreatorRetrievalConfig) -> None:
        self._config = config
        self._privacy = CreatorPrivacySanitizer()

    def plan(
        self,
        request: CreatorRetrievalRequest,
        *,
        retrieval_round: int,
        previous_grade: CreatorEvidenceGrade | None = None,
    ) -> CreatorRetrievalPlan:
        if not _as_bool(request.source_scope.get("include_community_posts", True)):
            return self._skip(
                retrieval_round,
                "Community post retrieval is disabled by source_scope.",
            )

        intent = _intent(request)
        if intent == RetrievalIntent.SKIP:
            return self._skip(
                retrieval_round,
                "The task explicitly disabled research retrieval.",
            )

        allowed = _allowed_channels(request.source_scope)
        channels = _channels_for_intent(intent)
        if retrieval_round > 1:
            channels = (
                RetrievalChannel.QDRANT,
                RetrievalChannel.SQL,
            )
        channels = tuple(channel for channel in channels if channel in allowed)
        if not channels and allowed:
            channels = tuple(
                channel
                for channel in (
                    RetrievalChannel.QDRANT,
                    RetrievalChannel.SQL,
                )
                if channel in allowed
            )
        channels = _apply_channel_flags(channels, request.source_scope)
        if not channels:
            return self._skip(
                retrieval_round,
                "No retrieval channel is authorized by source_scope.",
            )

        queries = self._queries(
            request,
            intent=intent,
            retrieval_round=retrieval_round,
            previous_grade=previous_grade,
        )
        if not queries:
            return self._skip(
                retrieval_round,
                "No safe retrieval query could be produced.",
            )

        return CreatorRetrievalPlan(
            retrieval_round=retrieval_round,
            intent=intent,
            queries=queries,
            channels=channels,
            filters=_filters(request.source_scope),
            candidate_top_k=self._config.candidate_top_k,
            final_top_k=self._config.final_top_k,
            require_sql_hydration=True,
            reason=(
                f"Round {retrieval_round} selected {', '.join(item.value for item in channels)} "
                f"for {intent.value}."
            ),
        )

    def _queries(
        self,
        request: CreatorRetrievalRequest,
        *,
        intent: RetrievalIntent,
        retrieval_round: int,
        previous_grade: CreatorEvidenceGrade | None,
    ) -> tuple[str, ...]:
        raw: list[str] = []
        configured = request.constraints.get("research_queries")
        if isinstance(configured, (list, tuple)):
            raw.extend(str(value) for value in configured)
        if retrieval_round > 1 and previous_grade is not None:
            raw.extend(
                _rewritten_query(topic, intent)
                for topic in previous_grade.missing_topics
            )
        raw.append(request.goal)
        tags = _string_values(request.source_scope.get("tags"), limit=20)
        if retrieval_round > 1 and tags:
            raw.append(f"{request.goal} {' '.join(tags)}")

        queries: list[str] = []
        seen: set[str] = set()
        for value in raw:
            sanitized = self._privacy.sanitize(value)
            normalized = re.sub(r"\s+", " ", sanitized).strip()[:500]
            key = normalized.casefold()
            if normalized and key not in seen:
                seen.add(key)
                queries.append(normalized)
            if len(queries) >= self._config.max_queries_per_round:
                break
        return tuple(queries)

    @staticmethod
    def _skip(retrieval_round: int, reason: str) -> CreatorRetrievalPlan:
        return CreatorRetrievalPlan(
            retrieval_round=retrieval_round,
            intent=RetrievalIntent.SKIP,
            reason=reason,
        )


def _intent(request: CreatorRetrievalRequest) -> RetrievalIntent:
    configured = str(request.constraints.get("retrieval_intent") or "").strip().upper()
    aliases = {
        "NONE": RetrievalIntent.SKIP,
        "SKIP": RetrievalIntent.SKIP,
        "TOPIC": RetrievalIntent.TOPIC_RESEARCH,
        "TOPIC_RESEARCH": RetrievalIntent.TOPIC_RESEARCH,
        "TREND": RetrievalIntent.TREND_DISCOVERY,
        "TREND_DISCOVERY": RetrievalIntent.TREND_DISCOVERY,
        "FACT": RetrievalIntent.FACT_CHECK,
        "FACT_CHECK": RetrievalIntent.FACT_CHECK,
        "PERFORMANCE": RetrievalIntent.PERFORMANCE_ANALYSIS,
        "PERFORMANCE_ANALYSIS": RetrievalIntent.PERFORMANCE_ANALYSIS,
    }
    if configured in aliases:
        return aliases[configured]

    goal = request.goal.casefold()
    if any(
        marker in goal
        for marker in ("热点", "趋势", "热度", "trending", "trend", "hot topic")
    ):
        return RetrievalIntent.TREND_DISCOVERY
    if any(
        marker in goal
        for marker in ("核实", "事实", "数据", "fact check", "verify", "citation")
    ):
        return RetrievalIntent.FACT_CHECK
    if request.task_kind == CreatorTaskKind.ANALYZE_CONTENT:
        return RetrievalIntent.PERFORMANCE_ANALYSIS
    return RetrievalIntent.TOPIC_RESEARCH


def _channels_for_intent(
    intent: RetrievalIntent,
) -> tuple[RetrievalChannel, ...]:
    return {
        RetrievalIntent.TOPIC_RESEARCH: (
            RetrievalChannel.QDRANT,
            RetrievalChannel.SQL,
        ),
        RetrievalIntent.TREND_DISCOVERY: (
            RetrievalChannel.SQL,
            RetrievalChannel.QDRANT,
        ),
        RetrievalIntent.FACT_CHECK: (
            RetrievalChannel.QDRANT,
            RetrievalChannel.SQL,
        ),
        RetrievalIntent.PERFORMANCE_ANALYSIS: (RetrievalChannel.SQL,),
        RetrievalIntent.SKIP: (),
    }[intent]


def _rewritten_query(topic: str, intent: RetrievalIntent) -> str:
    suffix = {
        RetrievalIntent.TOPIC_RESEARCH: "evidence examples implementation",
        RetrievalIntent.TREND_DISCOVERY: "latest trend discussion",
        RetrievalIntent.FACT_CHECK: "source data evidence",
        RetrievalIntent.PERFORMANCE_ANALYSIS: "engagement metrics analysis",
        RetrievalIntent.SKIP: "",
    }[intent]
    return f"{topic} {suffix}".strip()


def _allowed_channels(source_scope: dict[str, Any]) -> set[RetrievalChannel]:
    if "retrieval_sources" not in source_scope:
        return set(RetrievalChannel)
    values = _string_values(source_scope.get("retrieval_sources"), limit=10)
    return {
        channel
        for value in values
        if (channel := _CHANNEL_ALIASES.get(value.strip().upper())) is not None
    }


def _apply_channel_flags(
    channels: tuple[RetrievalChannel, ...],
    source_scope: dict[str, Any],
) -> tuple[RetrievalChannel, ...]:
    flags = {
        RetrievalChannel.QDRANT: "include_vector_search",
        RetrievalChannel.SQL: "include_business_data",
    }
    return tuple(
        channel
        for channel in channels
        if _as_bool(source_scope.get(flags[channel], True))
    )


def _filters(source_scope: dict[str, Any]) -> CreatorRetrievalFilters:
    data: dict[str, Any] = {
        "tags": _string_values(source_scope.get("tags"), limit=20),
        "creator_ids": _string_values(
            source_scope.get("creator_ids"),
            limit=20,
        ),
        "content_types": _string_values(
            source_scope.get("content_types"),
            limit=20,
        ),
    }
    for source_key, target_key in (
        ("published_after", "published_after"),
        ("published_before", "published_before"),
    ):
        value = source_scope.get(source_key)
        if isinstance(value, (str, datetime)) and value:
            data[target_key] = value
    return CreatorRetrievalFilters.model_validate(data)


def _string_values(value: Any, *, limit: int) -> tuple[str, ...]:
    values: Iterable[Any]
    if isinstance(value, str):
        values = value.split(",")
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = value
    else:
        return ()
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw).strip()[:128]
        key = item.casefold()
        if item and key not in seen:
            seen.add(key)
            result.append(item)
        if len(result) >= limit:
            break
    return tuple(result)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)
