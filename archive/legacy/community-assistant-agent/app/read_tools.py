"""First-class read-tool handlers for Phase 5 Step 2 (no Worker dependency)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from app.clients import CapabilityGrant, CommunityClient
from app.search_retrieval import search_with_fallback
from app.tool_runtime import (
    MIGRATED_READ_TOOLS,
    ToolAttemptTrace,
    ToolCredentials,
    ToolInvocationContext,
)
from app.tools import ToolDefinition

logger = logging.getLogger(__name__)


class ScheduleLookup(Protocol):
    async def get_own_schedule(
        self, *, action_id: str, user_id: str
    ) -> dict[str, Any]:
        """Return schedule fields or raise ValueError if missing/unauthorized."""


class CapabilityProvider(Protocol):
    async def issue(
        self,
        *,
        action: str,
        resources: list[str],
        max_uses: int,
        ttl_seconds: int,
        context: ToolInvocationContext,
        credentials: ToolCredentials,
    ) -> CapabilityGrant: ...


@dataclass
class CommunityCapabilityProvider:
    community: CommunityClient

    async def issue(
        self,
        *,
        action: str,
        resources: list[str],
        max_uses: int,
        ttl_seconds: int,
        context: ToolInvocationContext,
        credentials: ToolCredentials,
    ) -> CapabilityGrant:
        return await self.community.issue_capability(
            access_token=credentials.access_token,
            run_id=context.run_id,
            actions=[action],
            resources=resources,
            ttl_seconds=ttl_seconds,
            max_uses=max_uses,
            trace_id=credentials.trace_id,
        )


def _ensure_deadline(deadline_at: datetime | None) -> None:
    if deadline_at is None:
        return
    if datetime.now(timezone.utc) >= deadline_at:
        raise TimeoutError("tool invocation deadline exceeded")


def _record_internal_call(attempt_trace: ToolAttemptTrace | None) -> None:
    if attempt_trace is not None:
        attempt_trace.internal_call_count += 1


async def handle_list_own_posts(
    *,
    community: CommunityClient,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    definition: ToolDefinition,
    capability: CapabilityGrant | None,
    credentials: ToolCredentials,
    deadline_at: datetime | None,
    attempt_trace: ToolAttemptTrace | None,
    **_kwargs: Any,
) -> dict[str, Any]:
    del context
    if capability is None:
        raise PermissionError("community.list_own_posts requires a capability grant")
    max_items = int(arguments["max_items"])
    budget = max(1, int(definition.capability_budget.max_internal_calls))
    collected: list[dict[str, Any]] = []
    offset = 0
    stopped_for_budget = False
    while len(collected) <= max_items:
        _ensure_deadline(deadline_at)
        if attempt_trace is not None and attempt_trace.internal_call_count >= budget:
            stopped_for_budget = True
            break
        page_limit = min(100, max_items + 1 - len(collected))
        if page_limit <= 0:
            break
        _record_internal_call(attempt_trace)
        page = await community.list_own_posts(
            limit=page_limit,
            offset=offset,
            capability_token=capability.token,
            trace_id=credentials.trace_id,
        )
        collected.extend(page)
        offset += len(page)
        if len(page) < page_limit or not page:
            break
    truncated = len(collected) > max_items or stopped_for_budget
    posts = collected[:max_items]
    return {
        "posts": posts,
        "count": len(posts),
        "truncated": truncated,
    }


async def handle_search_posts(
    *,
    community: CommunityClient,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    definition: ToolDefinition,
    capability: CapabilityGrant | None,
    credentials: ToolCredentials,
    deadline_at: datetime | None,
    attempt_trace: ToolAttemptTrace | None,
    **_kwargs: Any,
) -> dict[str, Any]:
    if capability is None:
        raise PermissionError("community.search_posts requires a capability grant")
    query = str(arguments["query"])
    limit = int(arguments["limit"])
    budget = max(1, int(definition.capability_budget.max_internal_calls))
    calls = {"count": 0}

    async def search(candidate: str, bounded_limit: int) -> list[dict[str, Any]]:
        _ensure_deadline(deadline_at)
        if calls["count"] >= budget:
            # Do not issue a second grant and do not call upstream without budget.
            return []
        calls["count"] += 1
        _record_internal_call(attempt_trace)
        return await community.search_posts(
            candidate,
            bounded_limit,
            capability_token=capability.token,
            trace_id=credentials.trace_id,
        )

    retrieval = await search_with_fallback(
        query,
        limit,
        search,
        max_candidates=budget,
    )
    fallback_used = len(retrieval.attempted_queries) > 1
    # Budget stopped further candidates while still empty / incomplete.
    budget_exhausted = (
        calls["count"] >= budget
        and (
            not retrieval.results
            or len(retrieval.attempted_queries) >= budget
        )
        and calls["count"] > 0
    )
    # More precise: stopped because budget blocked an additional candidate need.
    plan_would_continue = (
        not retrieval.results
        and calls["count"] >= budget
    )
    budget_exhausted = plan_would_continue
    if attempt_trace is not None:
        attempt_trace.metadata.update(
            {
                "fallback_used": fallback_used,
                "query_variant_count": len(retrieval.attempted_queries),
                "result_count": len(retrieval.results),
                "budget_requested": budget,
                "internal_calls_consumed": calls["count"],
                "search_complete": not budget_exhausted,
                "stop_reason": (
                    "CAPABILITY_BUDGET_EXHAUSTED" if budget_exhausted else None
                ),
            }
        )
    if fallback_used:
        logger.info(
            "community search broadened run_id=%s original=%r matched=%r attempts=%s",
            context.run_id,
            retrieval.original_query,
            retrieval.matched_query,
            retrieval.attempted_queries,
        )
    return {
        "query": retrieval.original_query,
        "results": list(retrieval.results),
        "truncated": budget_exhausted,
        "search_complete": not budget_exhausted,
        "stop_reason": (
            "CAPABILITY_BUDGET_EXHAUSTED" if budget_exhausted else None
        ),
    }


async def handle_get_schedule(
    *,
    schedule_lookup: ScheduleLookup,
    arguments: dict[str, Any],
    context: ToolInvocationContext,
    definition: ToolDefinition,
    capability: CapabilityGrant | None,
    credentials: ToolCredentials,
    deadline_at: datetime | None,
    attempt_trace: ToolAttemptTrace | None,
    **_kwargs: Any,
) -> dict[str, Any]:
    del definition, capability, credentials
    _ensure_deadline(deadline_at)
    _record_internal_call(attempt_trace)
    user_id = context.user_id
    return await schedule_lookup.get_own_schedule(
        action_id=str(arguments["action_id"]),
        user_id=user_id,
    )


def register_migrated_read_handlers(
    runtime: Any,
    *,
    community: CommunityClient,
    schedule_lookup: ScheduleLookup,
) -> None:
    """Install instance-local handlers for the three Step 2 read tools."""

    async def list_own_posts(**kwargs: Any) -> dict[str, Any]:
        return await handle_list_own_posts(community=community, **kwargs)

    async def search_posts(**kwargs: Any) -> dict[str, Any]:
        return await handle_search_posts(community=community, **kwargs)

    async def get_schedule(**kwargs: Any) -> dict[str, Any]:
        return await handle_get_schedule(schedule_lookup=schedule_lookup, **kwargs)

    runtime.register_or_replace_handler("community.list_own_posts", list_own_posts)
    runtime.register_or_replace_handler("community.search_posts", search_posts)
    runtime.register_or_replace_handler("publication.get_schedule", get_schedule)
