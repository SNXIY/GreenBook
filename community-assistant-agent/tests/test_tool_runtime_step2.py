"""Phase 5 Step 2 — migrate first-batch read tools into ToolRuntime."""

from __future__ import annotations

import ast
import asyncio
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.clients import CapabilityGrant, CommunityClient
from app.config import Settings
from app.query_agent import QueryAgent
from app.read_tools import handle_search_posts, register_migrated_read_handlers
from app.router import ControlPlaneRouter
from app.tool_runtime import (
    LEGACY_BUILTIN_MIGRATION_BACKLOG,
    MIGRATED_READ_TOOLS,
    ToolCredentials,
    ToolErrorCode,
    ToolInvocationContext,
    ToolInvocationStatus,
    ToolRuntime,
)
from app.tools import CapabilityBudget, TransportType, tool_registry
from app.worker import AgentWorker

ROOT = Path(__file__).resolve().parents[1]


def _ctx(**overrides: Any) -> ToolInvocationContext:
    payload = {
        "run_id": "run-1",
        "user_id": "user-1",
        "tenant_id": "zhiguang",
        "conversation_id": "conv-1",
        "request_id": "req-1",
        "operation_key": "op-1",
        "idempotency_key": "op-1",
        "attempt": 1,
    }
    payload.update(overrides)
    return ToolInvocationContext(**payload)


def _creds() -> ToolCredentials:
    return ToolCredentials(access_token="jwt-test", trace_id="trace-1")


class RecordingCapabilityProvider:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def issue(self, **kwargs: Any) -> CapabilityGrant:
        self.calls.append(
            {
                "action": kwargs["action"],
                "max_uses": kwargs["max_uses"],
                "run_id": kwargs["context"].run_id,
            }
        )
        return CapabilityGrant(
            token=f"cap-{len(self.calls)}",
            capability_id=f"id-{len(self.calls)}",
            expires_at="2099-01-01T00:00:00Z",
        )


def _settings(**overrides: Any) -> Settings:
    values = {
        "java_base_url": "http://java.test",
        "service_shared_secret": "secret",
        "tool_http_trust_env": False,
        "DEEPSEEK_API_KEY": "test-key",
    }
    values.update(overrides)
    return Settings(**values)


def _runtime_with_transport(
    transport: httpx.MockTransport,
    *,
    capability: RecordingCapabilityProvider | None = None,
    schedule_lookup: Any | None = None,
) -> tuple[ToolRuntime, CommunityClient, RecordingCapabilityProvider]:
    settings = _settings()
    community = CommunityClient(settings, transport=transport)
    provider = capability or RecordingCapabilityProvider()
    runtime = ToolRuntime(definitions=tool_registry, capability_provider=provider)

    class _Lookup:
        async def get_own_schedule(
            self, *, action_id: str, user_id: str
        ) -> dict[str, Any]:
            if schedule_lookup is not None:
                return await schedule_lookup(action_id=action_id, user_id=user_id)
            return {
                "action_id": action_id,
                "draft_id": "draft-1",
                "run_at": "2026-08-05T00:10:00+00:00",
                "status": "SCHEDULED",
            }

    register_migrated_read_handlers(
        runtime,
        community=community,
        schedule_lookup=_Lookup(),
    )
    return runtime, community, provider


@pytest.mark.asyncio
async def test_list_own_posts_success_via_runtime() -> None:
    provider = RecordingCapabilityProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/mine" not in str(request.url):
            return httpx.Response(404)
        assert request.headers.get("X-Assistant-Capability") == "cap-1"
        return httpx.Response(
            200,
            json=[{"id": "1", "title": "Hello", "status": "published"}],
        )

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    result = await runtime.invoke(
        tool_name="community.list_own_posts",
        arguments={"max_items": 10},
        context=_ctx(request_id="list-ok"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.output["count"] == 1
    assert provider.calls[0]["max_uses"] == 5
    trace = runtime.last_trace(result.trace_id)
    assert trace is not None
    assert trace.attempts[0].internal_call_count == 1
    assert "community.list_own_posts" not in LEGACY_BUILTIN_MIGRATION_BACKLOG
    assert "community.list_own_posts" not in inspect.getsource(
        AgentWorker._dispatch_builtin_tool
    )
    await community.close()


@pytest.mark.asyncio
async def test_list_own_posts_retries_after_503() -> None:
    hits = {"n": 0}
    provider = RecordingCapabilityProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/mine" not in str(request.url):
            return httpx.Response(404)
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(503, json={"error": "busy"})
        return httpx.Response(200, json=[])

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    result = await runtime.invoke(
        tool_name="community.list_own_posts",
        arguments={"max_items": 10},
        context=_ctx(request_id="list-503"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.attempts == 2
    assert result.trace_id == "list-503"
    trace = runtime.last_trace(result.trace_id)
    assert trace is not None
    assert len(trace.attempts) == 2
    assert hits["n"] == 2
    await community.close()


@pytest.mark.asyncio
async def test_search_original_query_success() -> None:
    queries: list[str] = []
    provider = RecordingCapabilityProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/search" not in str(request.url):
            return httpx.Response(404)
        q = parse_qs(urlparse(str(request.url)).query).get("q", [""])[0]
        queries.append(q)
        return httpx.Response(
            200,
            json=[{"id": "p1", "title": "Agent", "creatorId": "u1"}],
        )

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "Agent", "limit": 5},
        context=_ctx(request_id="search-ok"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert len(queries) == 1
    assert provider.calls[0]["max_uses"] == 5
    trace = runtime.last_trace(result.trace_id)
    assert trace.attempts[0].metadata.get("fallback_used") is False
    assert trace.attempts[0].internal_call_count == 1
    await community.close()


@pytest.mark.asyncio
async def test_search_fallback_shares_one_capability_grant() -> None:
    queries: list[str] = []
    provider = RecordingCapabilityProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/search" not in str(request.url):
            return httpx.Response(404)
        q = parse_qs(urlparse(str(request.url)).query).get("q", [""])[0]
        queries.append(q)
        assert request.headers.get("X-Assistant-Capability") == "cap-1"
        if q == "如何学好agent":
            return httpx.Response(200, json=[])
        return httpx.Response(
            200,
            json=[{"id": "p1", "title": "agent", "creatorId": "u1"}],
        )

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "如何学好agent", "limit": 5},
        context=_ctx(request_id="search-fb"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert len(queries) >= 2
    assert len(provider.calls) == 1
    trace = runtime.last_trace(result.trace_id)
    assert trace.attempts[0].metadata.get("fallback_used") is True
    assert trace.attempts[0].internal_call_count >= 2
    await community.close()


@pytest.mark.asyncio
async def test_search_budget_exhausted_returns_empty_without_second_grant() -> None:
    provider = RecordingCapabilityProvider()
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/search" not in str(request.url):
            return httpx.Response(404)
        q = parse_qs(urlparse(str(request.url)).query).get("q", [""])[0]
        queries.append(q)
        return httpx.Response(200, json=[])

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    original = tool_registry.get("community.search_posts")

    async def limited(**kwargs: Any) -> dict[str, Any]:
        kwargs["definition"] = replace(
            original,
            capability_budget=CapabilityBudget(base_uses=1, max_internal_calls=1),
        )
        return await handle_search_posts(community=community, **kwargs)

    runtime.register_or_replace_handler("community.search_posts", limited)

    async def issue_one(**kwargs: Any) -> CapabilityGrant:
        kwargs = dict(kwargs)
        kwargs["max_uses"] = 1
        return await RecordingCapabilityProvider.issue(provider, **kwargs)

    provider.issue = issue_one  # type: ignore[method-assign]

    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "如何学好agent", "limit": 5},
        context=_ctx(request_id="search-budget"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.output["results"] == []
    assert len(queries) == 1
    assert len(provider.calls) == 1
    await community.close()


@pytest.mark.asyncio
async def test_search_deadline_stops_fallback() -> None:
    provider = RecordingCapabilityProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/search" not in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json=[])

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "如何学好agent", "limit": 5},
        context=_ctx(request_id="search-deadline", deadline_at=past),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.RETRYABLE_FAILURE
    assert result.error_code == ToolErrorCode.TIMEOUT.value
    await community.close()


@pytest.mark.asyncio
async def test_get_schedule_success_via_builtin_transport() -> None:
    provider = RecordingCapabilityProvider()

    async def lookup(*, action_id: str, user_id: str) -> dict[str, Any]:
        assert action_id == "sched-1"
        assert user_id == "user-1"
        return {
            "action_id": "sched-1",
            "draft_id": "draft-9",
            "run_at": "2026-08-05T00:10:00+00:00",
            "status": "SCHEDULED",
        }

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(lambda _r: httpx.Response(404)),
        capability=provider,
        schedule_lookup=lookup,
    )
    result = await runtime.invoke(
        tool_name="publication.get_schedule",
        arguments={"action_id": "sched-1"},
        context=_ctx(request_id="sched-ok"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.output["draft_id"] == "draft-9"
    assert provider.calls == []
    assert "publication.get_schedule" not in inspect.getsource(
        AgentWorker._dispatch_builtin_tool
    )
    await community.close()


@pytest.mark.asyncio
async def test_list_own_posts_respects_retry_after() -> None:
    hits = {"n": 0}
    provider = RecordingCapabilityProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/mine" not in str(request.url):
            return httpx.Response(404)
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"error": "rate"}
            )
        return httpx.Response(200, json=[])

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    result = await runtime.invoke(
        tool_name="community.list_own_posts",
        arguments={"max_items": 5},
        context=_ctx(request_id="retry-after"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.attempts == 2
    trace = runtime.last_trace(result.trace_id)
    assert trace.attempts[0].retry_after_ms == 0
    await community.close()


@pytest.mark.asyncio
async def test_capability_401_is_not_retried_as_network_error() -> None:
    provider = RecordingCapabilityProvider()
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "/posts/mine" not in str(request.url):
            return httpx.Response(404)
        hits["n"] += 1
        return httpx.Response(401, text="capability rejected")

    runtime, community, _ = _runtime_with_transport(
        httpx.MockTransport(handler), capability=provider
    )
    result = await runtime.invoke(
        tool_name="community.list_own_posts",
        arguments={"max_items": 5},
        context=_ctx(request_id="cap-401"),
        credentials=_creds(),
        skip_policy=True,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.DENIED
    assert result.error_code in {
        ToolErrorCode.CAPABILITY_DENIED.value,
        ToolErrorCode.AUTHENTICATION_ERROR.value,
    }
    assert result.attempts == 1
    assert hits["n"] == 1
    await community.close()


@pytest.mark.asyncio
async def test_runtime_transport_isolation_parallel() -> None:
    def make_handler(label: str):
        def handler(request: httpx.Request) -> httpx.Response:
            if "/posts/mine" not in str(request.url):
                return httpx.Response(404)
            return httpx.Response(
                200,
                json=[{"id": label, "title": label, "status": "published"}],
            )

        return handler

    runtime_a, community_a, _ = _runtime_with_transport(
        httpx.MockTransport(make_handler("A"))
    )
    runtime_b, community_b, _ = _runtime_with_transport(
        httpx.MockTransport(make_handler("B"))
    )
    result_a, result_b = await asyncio.gather(
        runtime_a.invoke(
            tool_name="community.list_own_posts",
            arguments={"max_items": 5},
            context=_ctx(request_id="iso-a"),
            credentials=_creds(),
            skip_policy=True,
            raise_on_failure=False,
        ),
        runtime_b.invoke(
            tool_name="community.list_own_posts",
            arguments={"max_items": 5},
            context=_ctx(request_id="iso-b"),
            credentials=_creds(),
            skip_policy=True,
            raise_on_failure=False,
        ),
    )
    assert result_a.output["posts"][0]["id"] == "A"
    assert result_b.output["posts"][0]["id"] == "B"
    await community_a.close()
    await community_b.close()


@pytest.mark.asyncio
async def test_query_agent_count_still_uses_list_own_posts_via_executor() -> None:
    agent = QueryAgent()
    calls: list[tuple[str, dict]] = []

    async def execute_tool(name: str, args: dict) -> dict:
        calls.append((name, args))
        return {"posts": [{"id": "1"}, {"id": "2"}], "count": 2, "truncated": False}

    result = await agent.handle(
        message="我发布了多少帖子",
        execute_tool=execute_tool,
    )
    assert result.kind == "OWN_POST_COUNT"
    assert result.created_goal is False
    assert calls == [("community.list_own_posts", {"max_items": 1000})]


@pytest.mark.asyncio
async def test_query_agent_schedule_status_does_not_create_goal() -> None:
    agent = QueryAgent()
    result = await agent.handle(
        message="查询这个定时任务状态",
        schedules=[
            {
                "action_id": "sched-1",
                "draft_id": "d1",
                "run_at": "2026-08-05T00:10:00Z",
                "status": "SCHEDULED",
            }
        ],
    )
    assert result.kind == "SCHEDULE_STATUS"
    assert result.created_goal is False
    assert result.tool_name is None


def test_query_recent_posts_route_stays_query() -> None:
    route = ControlPlaneRouter().classify("最近发布了哪些帖子")
    assert route.mode == "QUERY"


def test_worker_dispatch_no_longer_contains_migrated_tools() -> None:
    source = inspect.getsource(AgentWorker._dispatch_builtin_tool)
    for name in MIGRATED_READ_TOOLS:
        assert name not in source
        assert name not in LEGACY_BUILTIN_MIGRATION_BACKLOG


def test_read_tools_module_does_not_import_worker() -> None:
    tree = ast.parse((ROOT / "app" / "read_tools.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[-1])
    assert "worker" not in imported


def test_query_agent_does_not_import_community_client() -> None:
    tree = ast.parse((ROOT / "app" / "query_agent.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[-1])
    assert "clients" not in imported
    assert "CommunityClient" not in imported


def test_read_tools_do_not_write_run_step_or_recurse_invoke() -> None:
    source = (ROOT / "app" / "read_tools.py").read_text(encoding="utf-8")
    assert "RunStep" not in source
    assert "ToolRuntime.invoke" not in source
    assert 'os.environ["HTTP_PROXY"]' not in source


def test_trace_models_do_not_hold_capability_token_fields() -> None:
    from app.tool_runtime import ToolAttemptTrace, ToolInvocationTrace

    assert "token" not in ToolAttemptTrace.__dataclass_fields__
    assert "capability" not in ToolAttemptTrace.__dataclass_fields__
    assert "token" not in ToolInvocationTrace.__dataclass_fields__


@pytest.mark.asyncio
async def test_community_client_defaults_trust_env_false() -> None:
    settings = _settings()
    assert settings.tool_http_trust_env is False
    client = CommunityClient(
        settings,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=[])),
    )
    assert client.http.trust_env is False
    await client.close()


def test_migrated_definitions_match_step2_contracts() -> None:
    search = tool_registry.get("community.search_posts")
    assert search.transport == TransportType.HTTP
    assert search.capability_budget.max_internal_calls == 5
    assert search.retry_policy.max_attempts == 2

    own = tool_registry.get("community.list_own_posts")
    assert own.transport == TransportType.HTTP
    assert own.capability_budget.max_internal_calls == 5

    sched = tool_registry.get("publication.get_schedule")
    assert sched.transport == TransportType.BUILTIN
    assert sched.capability_budget.max_internal_calls == 0
