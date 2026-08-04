"""Phase 5 Step 1 — ToolRuntime invoke skeleton, registry isolation, UNKNOWN writes."""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.tool_runtime import (
    LEGACY_BUILTIN_MIGRATION_BACKLOG,
    LegacyBuiltinTransport,
    ToolCredentials,
    ToolErrorCode,
    ToolInvocationContext,
    ToolInvocationStatus,
    ToolRuntime,
    ToolRuntimeError,
    UnknownSideEffectError,
    classify_tool_exception,
    create_tool_runtime,
)
from app.tools import (
    RiskLevel,
    SearchPostsOutput,
    ToolDefinition,
    ToolRegistry,
    TransportType,
    tool_registry,
)
from app.worker import AgentWorker, _is_transient_exception


ROOT = Path(__file__).resolve().parents[1]


def _ctx(
    *,
    run_id: str = "run-1",
    operation_key: str | None = "op-1",
    request_id: str = "req-1",
    attempt: int = 1,
) -> ToolInvocationContext:
    return ToolInvocationContext(
        run_id=run_id,
        user_id="user-1",
        tenant_id="zhiguang",
        conversation_id="conv-1",
        request_id=request_id,
        operation_key=operation_key,
        idempotency_key=operation_key,
        attempt=attempt,
    )


def _search_ok() -> dict[str, Any]:
    return {
        "query": "agent",
        "results": [
            {
                "id": "p1",
                "title": "Agent 入门",
                "creator_id": "u1",
            }
        ],
    }


# ---------------------------------------------------------------------------
# A. Runtime single entry + Legacy adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invoke_routes_through_legacy_executor() -> None:
    path: list[str] = []
    runtime = ToolRuntime(definitions=tool_registry)

    async def legacy_executor(**kwargs: Any) -> dict[str, Any]:
        path.append("legacy")
        assert kwargs["tool_name"] == "community.get_post"
        return {
            "id": "p1",
            "title": "T",
            "body_markdown": "body",
            "creator_id": "u1",
        }

    runtime.set_legacy_executor(legacy_executor)
    # Migrated reads bypass legacy_executor; use a still-legacy tool.
    result = await runtime.invoke(
        tool_name="community.get_post",
        arguments={"post_id": "p1"},
        context=_ctx(operation_key="op-get"),
        raise_on_failure=False,
    )
    assert path == ["legacy"]
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.trace_id


@pytest.mark.asyncio
async def test_legacy_builtin_transport_forwards_to_dispatch() -> None:
    calls: list[str] = []

    async def dispatch(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs["tool"])
        return _search_ok()

    transport = LegacyBuiltinTransport(dispatch)
    out = await transport.invoke(
        tool_name="community.search_posts",
        arguments={"query": "agent", "limit": 5},
        context=_ctx(operation_key="op-x"),
        run=object(),
        ordinal=1,
        timeout_seconds=15,
    )
    assert calls == ["community.search_posts"]
    assert out["query"] == "agent"


def test_worker_execute_tool_uses_execution_runtime_invoke() -> None:
    source = inspect.getsource(AgentWorker._execute_tool)
    assert "execution_runtime.invoke" in source
    assert "ToolInvocationContext" in source


# ---------------------------------------------------------------------------
# B. Input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_input_returns_validation_error_without_handler() -> None:
    calls = 0
    runtime = ToolRuntime(definitions=tool_registry)

    async def handler(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return _search_ok()

    runtime.register_handler("community.search_posts", handler)
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "", "limit": 5},
        context=_ctx(),
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.PERMANENT_FAILURE
    assert result.error_code == ToolErrorCode.VALIDATION_ERROR.value
    assert result.attempts == 0
    assert calls == 0
    assert runtime.last_trace(result.trace_id) is not None
    assert runtime.last_trace(result.trace_id).error_code == (
        ToolErrorCode.VALIDATION_ERROR.value
    )


# ---------------------------------------------------------------------------
# C. Output validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_output_returns_output_schema_error() -> None:
    runtime = ToolRuntime(definitions=tool_registry)

    async def bad_handler(**_kwargs: Any) -> dict[str, Any]:
        return {"query": "agent", "results": "not-a-list"}

    runtime.register_handler("community.search_posts", bad_handler)
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "agent", "limit": 5},
        context=_ctx(),
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.PERMANENT_FAILURE
    assert result.error_code == ToolErrorCode.OUTPUT_SCHEMA_ERROR.value
    assert result.output is None
    trace = runtime.last_trace(result.trace_id)
    assert trace is not None
    assert trace.error_code == ToolErrorCode.OUTPUT_SCHEMA_ERROR.value
    assert "token" not in str(trace.argument_summary).lower()


# ---------------------------------------------------------------------------
# D. Registry isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_handler_isolation_without_handlers_clear() -> None:
    runtime_a = create_tool_runtime(definitions=tool_registry)
    runtime_b = create_tool_runtime(definitions=tool_registry)

    async def handler_a(**_kwargs: Any) -> dict[str, Any]:
        return {
            "query": "from-a",
            "results": [{"id": "a1", "title": "A", "creator_id": "u"}],
        }

    async def handler_b(**_kwargs: Any) -> dict[str, Any]:
        return {
            "query": "from-b",
            "results": [{"id": "b1", "title": "B", "creator_id": "u"}],
        }

    runtime_a.register_handler("community.search_posts", handler_a)
    runtime_b.register_handler("community.search_posts", handler_b)

    result_a = await runtime_a.invoke(
        tool_name="community.search_posts",
        arguments={"query": "x", "limit": 5},
        context=_ctx(request_id="a"),
        raise_on_failure=False,
    )
    result_b = await runtime_b.invoke(
        tool_name="community.search_posts",
        arguments={"query": "x", "limit": 5},
        context=_ctx(request_id="b"),
        raise_on_failure=False,
    )
    assert result_a.output["query"] == "from-a"
    assert result_b.output["query"] == "from-b"
    # Shared definition registry must not be the mutable handler bank.
    assert tool_registry.handler_for("community.search_posts") is None


def test_staged_handlers_drain_into_runtime() -> None:
    registry = ToolRegistry(
        [
            ToolDefinition(
                "community.search_posts",
                "检索社区",
                "test",
                tool_registry.get("community.search_posts").arguments_model,
                SearchPostsOutput,
                RiskLevel.READ,
                15,
            )
        ]
    )

    async def staged(**_kwargs: Any) -> dict[str, Any]:
        return _search_ok()

    registry.register_handler("community.search_posts", staged)
    runtime = ToolRuntime(definitions=registry)
    adopted = runtime.adopt_staged_handlers(registry)
    assert adopted == 1
    assert registry.handler_for("community.search_posts") is None
    assert runtime.handler_for("community.search_posts") is staged


# ---------------------------------------------------------------------------
# E. Trace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_trace_has_ids_and_duration() -> None:
    runtime = ToolRuntime(definitions=tool_registry)

    async def handler(**_kwargs: Any) -> dict[str, Any]:
        return _search_ok()

    runtime.register_handler("community.search_posts", handler)
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "agent", "limit": 5},
        context=_ctx(run_id="run-trace"),
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert result.duration_ms >= 0
    trace = runtime.last_trace(result.trace_id)
    assert trace is not None
    assert trace.run_id == "run-trace"
    assert trace.tool_name == "community.search_posts"
    assert trace.status == "SUCCESS"
    assert trace.duration_ms is not None
    assert "capability" not in trace.argument_summary
    assert "token" not in trace.argument_summary


@pytest.mark.asyncio
async def test_failure_trace_records_error_code_without_secrets() -> None:
    runtime = ToolRuntime(definitions=tool_registry)
    result = await runtime.invoke(
        tool_name="community.search_posts",
        arguments={"query": "", "limit": 5},
        context=_ctx(
            request_id="req-fail",
            operation_key="op-fail",
        ),
        raise_on_failure=False,
    )
    trace = runtime.last_trace(result.trace_id)
    assert trace is not None
    assert trace.error_code == ToolErrorCode.VALIDATION_ERROR.value
    dumped = str(trace.__dict__)
    assert "Bearer" not in dumped
    assert "capability_token" not in dumped


# ---------------------------------------------------------------------------
# F. Legacy behavior regression (schema + invoke round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "output"),
    [
        (
            "community.list_own_posts",
            {"max_items": 10},
            {"posts": [], "count": 0, "truncated": False},
        ),
        (
            "community.search_posts",
            {"query": "agent", "limit": 5},
            _search_ok(),
        ),
        (
            "publication.get_schedule",
            {"action_id": "sched-1"},
            {
                "action_id": "sched-1",
                "draft_id": "draft-1",
                "run_at": datetime(2026, 8, 5, 0, 10, tzinfo=timezone.utc),
                "status": "SCHEDULED",
            },
        ),
        (
            "publication.update_schedule",
            {"action_id": "sched-1", "delay_seconds": 600},
            {
                "action_id": "sched-1",
                "draft_id": "draft-1",
                "run_at": datetime(2026, 8, 5, 0, 20, tzinfo=timezone.utc),
                "status": "SCHEDULED",
            },
        ),
    ],
)
async def test_tool_round_trip_preserves_output_contract(
    tool_name: str,
    arguments: dict[str, Any],
    output: dict[str, Any],
) -> None:
    from app.tool_runtime import MIGRATED_READ_TOOLS, MIGRATED_WRITE_TOOLS

    runtime = ToolRuntime(definitions=tool_registry)
    calls = 0

    async def handler(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return output

    if tool_name in MIGRATED_READ_TOOLS or tool_name in MIGRATED_WRITE_TOOLS:
        runtime.register_or_replace_handler(tool_name, handler)
    else:
        runtime.register_handler(tool_name, handler)

        async def legacy(**kwargs: Any) -> dict[str, Any]:
            return await handler(**kwargs)

        runtime.set_legacy_executor(legacy)

    result = await runtime.invoke(
        tool_name=tool_name,
        arguments=arguments,
        context=_ctx(operation_key=f"op-{tool_name}"),
        credentials=(
            ToolCredentials(access_token="jwt-test", trace_id="trace-1")
            if tool_name in MIGRATED_WRITE_TOOLS
            else None
        ),
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.SUCCESS
    assert calls == 1
    assert result.output is not None
    if tool_name in MIGRATED_READ_TOOLS or tool_name in MIGRATED_WRITE_TOOLS:
        assert tool_name not in LEGACY_BUILTIN_MIGRATION_BACKLOG
        assert tool_registry.get(tool_name).transport != TransportType.LEGACY_BUILTIN
    else:
        assert tool_name in LEGACY_BUILTIN_MIGRATION_BACKLOG
        assert tool_registry.get(tool_name).transport == TransportType.LEGACY_BUILTIN


# ---------------------------------------------------------------------------
# G. UNKNOWN write model — no blind retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_timeout_is_unknown_without_second_call() -> None:
    runtime = ToolRuntime(definitions=tool_registry)
    calls = 0

    async def write_timeout(**_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise TimeoutError("upstream hung after accept")

    runtime.register_or_replace_handler("publication.update_schedule", write_timeout)
    context = _ctx(operation_key="assistant-effect-run-1-3")
    creds = ToolCredentials(access_token="jwt-test", trace_id="trace-1")
    result = await runtime.invoke(
        tool_name="publication.update_schedule",
        arguments={"action_id": "sched-1", "delay_seconds": 600},
        context=context,
        credentials=creds,
        raise_on_failure=False,
    )
    assert result.status == ToolInvocationStatus.UNKNOWN
    assert result.error_code == ToolErrorCode.UNKNOWN_SIDE_EFFECT.value
    assert calls == 1
    assert context.operation_key == "assistant-effect-run-1-3"

    with pytest.raises(ToolRuntimeError) as raised:
        await runtime.invoke(
            tool_name="publication.update_schedule",
            arguments={"action_id": "sched-1", "delay_seconds": 600},
            context=context,
            credentials=creds,
            raise_on_failure=True,
        )
    assert raised.value.status == ToolInvocationStatus.UNKNOWN
    assert raised.value.error_code == ToolErrorCode.UNKNOWN_SIDE_EFFECT.value
    assert calls == 2  # explicit second invoke by test, not Runtime auto-retry
    assert raised.value.operation_key == "assistant-effect-run-1-3"


def test_unknown_side_effect_is_not_transient_for_retry() -> None:
    error = UnknownSideEffectError(
        "write unknown",
        operation_key="assistant-effect-run-1-3",
    )
    assert _is_transient_exception(error) is False


def test_classify_read_timeout_vs_write_unknown() -> None:
    read_def = tool_registry.get("community.search_posts")
    write_def = tool_registry.get("publication.update_schedule")
    status_r, code_r = classify_tool_exception(TimeoutError("t"), definition=read_def)
    status_w, code_w = classify_tool_exception(TimeoutError("t"), definition=write_def)
    assert status_r == ToolInvocationStatus.RETRYABLE_FAILURE
    assert code_r == ToolErrorCode.TIMEOUT
    assert status_w == ToolInvocationStatus.UNKNOWN
    assert code_w == ToolErrorCode.UNKNOWN_SIDE_EFFECT


# ---------------------------------------------------------------------------
# Architecture constraints
# ---------------------------------------------------------------------------


def test_tool_runtime_does_not_import_planner() -> None:
    tree = ast.parse((ROOT / "app" / "tool_runtime.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[-1])
    banned = {"plan_compiler", "planner", "llm", "turn_plan", "intent_delta_plan_compiler"}
    assert banned.isdisjoint(imported)


def test_query_agent_still_avoids_http_client() -> None:
    tree = ast.parse((ROOT / "app" / "query_agent.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.add(node.module.split(".")[-1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    assert "httpx" not in imported
    assert "clients" not in imported


def test_planner_modules_do_not_issue_capability() -> None:
    for relative in ("app/plan_compiler.py", "app/turn_plan.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "issue_capability" not in source
        assert "_issue_capability" not in source


def test_resolvers_and_task_manager_do_not_call_tool_runtime_invoke() -> None:
    for relative in (
        "app/target_resolver.py",
        "app/task_manager.py",
        "app/temporal_resolver.py",
    ):
        path = ROOT / relative
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[-1])
                for alias in node.names:
                    imported.add(alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[-1])
        assert "tool_runtime" not in imported
        assert "ToolRuntime" not in imported
        source = path.read_text(encoding="utf-8")
        assert "execution_runtime.invoke" not in source
        assert "ToolRuntime.invoke" not in source


def test_tool_runtime_does_not_copy_dispatch_builtin_branches() -> None:
    source = (ROOT / "app" / "tool_runtime.py").read_text(encoding="utf-8")
    assert 'if tool == "community.search_posts"' not in source
    invoke_source = inspect.getsource(ToolRuntime.invoke)
    assert "community.search_posts" not in invoke_source


def test_worker_does_not_use_module_level_handlers_for_dispatch() -> None:
    source = inspect.getsource(AgentWorker._dispatch_tool)
    assert "execution_runtime.handler_for" in source
    assert "registry.handler_for" not in source


def test_tool_definition_defaults_preserve_legacy_transport_and_retry() -> None:
    legacy = tool_registry.get("community.get_post")
    assert legacy.transport == TransportType.LEGACY_BUILTIN
    assert legacy.retry_policy.max_attempts == 1
    assert legacy.idempotency_mode.value == "NONE"

    search = tool_registry.get("community.search_posts")
    assert search.transport == TransportType.HTTP
    assert search.retry_policy.max_attempts == 2
    assert search.capability_budget.max_internal_calls == 5
