"""Phase 4.3 tests for ToolRuntime, Ledger, and InvocationContext."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from greenbook_assistant_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)
from greenbook_assistant_core.execution.runtime.ledger import (
    InvocationStatus,
    ToolExecutionLedger,
)
from greenbook_assistant_core.execution.runtime.tool_runtime import (
    InvocationResult,
    ToolRuntime,
)


# ── helpers ──────────────────────────────────────────────────────

def _ctx(**kw: Any) -> ToolInvocationContext:
    return ToolInvocationContext.build(
        task_id=kw.pop("task_id", "t1"),
        execution_id=kw.pop("execution_id", "e1"),
        step_id=kw.pop("step_id", "s1"),
        capability=kw.pop("capability", "SEARCH_COMMUNITY"),
        tool_name=kw.pop("tool_name", "community.search_public_posts"),
        tool_args=kw.pop("tool_args", {"query": "Java"}),
        timeout_seconds=kw.pop("timeout_seconds", 5.0),
        **kw,
    )


def _runtime(responses: dict[str, dict[str, Any]] | None = None) -> ToolRuntime:
    """Build a ToolRuntime with canned responses."""
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        if responses and tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": True, "code": "", "data": {"echo": tool_args}}
    return ToolRuntime(handler)


# ── Scenario 1: normal tool call ──────────────────────────────────

@pytest.mark.asyncio
async def test_normal_tool_call() -> None:
    runtime = _runtime()
    ctx = _ctx()
    result = await runtime.invoke(ctx)

    assert result.ok is True
    assert result.tool_name == "community.search_public_posts"
    assert result.duration_ms >= 0  # may be 0 for instantaneous calls
    assert result.replayed is False


@pytest.mark.asyncio
async def test_normal_call_is_recorded_in_ledger() -> None:
    runtime = _runtime()
    ctx = _ctx()
    await runtime.invoke(ctx)

    entry = runtime.ledger.find_by_id(ctx.invocation_id)
    assert entry is not None
    assert entry.status == InvocationStatus.COMPLETED
    assert entry.tool_name == "community.search_public_posts"


# ── Scenario 2: timeout ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout_returns_error() -> None:
    async def slow_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"ok": True, "code": "", "data": {}}

    runtime = ToolRuntime(slow_handler)
    ctx = _ctx(timeout_seconds=0.1)

    result = await runtime.invoke(ctx)
    assert result.ok is False
    assert result.error_code == "TIMEOUT"
    assert result.retryable is True


@pytest.mark.asyncio
async def test_timeout_recorded_in_ledger() -> None:
    async def slow_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(10)
        return {"ok": True, "code": "", "data": {}}

    runtime = ToolRuntime(slow_handler)
    ctx = _ctx(timeout_seconds=0.1)
    await runtime.invoke(ctx)

    entry = runtime.ledger.find_by_id(ctx.invocation_id)
    assert entry is not None
    assert entry.status == InvocationStatus.TIMEOUT
    assert entry.error_code == "TIMEOUT"


# ── Scenario 3: retryable error ──────────────────────────────────

@pytest.mark.asyncio
async def test_retryable_error_recorded_as_failure() -> None:
    runtime = _runtime({
        "community.search_public_posts": {
            "ok": False, "code": "JAVA_BACKEND_UNAVAILABLE",
            "retryable": True, "user_message": "Backend temporarily down",
        },
    })
    ctx = _ctx()
    result = await runtime.invoke(ctx)

    assert result.ok is False
    assert result.error_code == "JAVA_BACKEND_UNAVAILABLE"
    assert result.retryable is True

    entry = runtime.ledger.find_by_id(ctx.invocation_id)
    assert entry is not None
    assert entry.status == InvocationStatus.FAILED


# ── Scenario 4: idempotency key prevents duplicate execution ─────

@pytest.mark.asyncio
async def test_same_idempotency_key_replays_cached_result() -> None:
    call_count = 0

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"ok": True, "code": "", "data": {"count": call_count}}

    runtime = ToolRuntime(handler)
    ctx = _ctx()

    # First call
    r1 = await runtime.invoke(ctx)
    assert r1.ok is True
    assert r1.replayed is False
    assert call_count == 1

    # Second call with same idempotency key — replayed
    r2 = await runtime.invoke(ctx)
    assert r2.ok is True
    assert r2.replayed is True
    assert call_count == 1  # handler NOT called again


@pytest.mark.asyncio
async def test_different_keys_call_handler_independently() -> None:
    call_count = 0

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"ok": True, "code": "", "data": {}}

    runtime = ToolRuntime(handler)
    ctx1 = _ctx(step_id="s1")
    ctx2 = _ctx(step_id="s2")  # Different step → different idempotency key

    await runtime.invoke(ctx1)
    await runtime.invoke(ctx2)
    assert call_count == 2


# ── Scenario 5: invocation logging ───────────────────────────────

@pytest.mark.asyncio
async def test_ledger_lists_invocations_by_execution() -> None:
    runtime = _runtime()
    ctx1 = _ctx(execution_id="e1", step_id="s1")
    ctx2 = _ctx(execution_id="e1", step_id="s2")
    ctx3 = _ctx(execution_id="e2", step_id="s1")

    await runtime.invoke(ctx1)
    await runtime.invoke(ctx2)
    await runtime.invoke(ctx3)

    assert runtime.ledger.count() == 3
    e1_entries = runtime.ledger.list_by_execution("e1")
    assert len(e1_entries) == 2


@pytest.mark.asyncio
async def test_ledger_entry_has_timing() -> None:
    runtime = _runtime()
    ctx = _ctx()
    result = await runtime.invoke(ctx)

    entry = runtime.ledger.find_by_id(ctx.invocation_id)
    assert entry is not None
    assert result.duration_ms == entry.duration_ms
    assert entry.started_at != ""
    assert entry.finished_at != ""


# ── edge cases ────────────────────────────────────────────────────

def test_context_builds_stable_idempotency_key() -> None:
    ctx1 = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        tool_name="community.search_public_posts",
    )
    ctx2 = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        tool_name="community.search_public_posts",
    )
    assert ctx1.idempotency_key == ctx2.idempotency_key


def test_context_different_args_same_key() -> None:
    """Idempotency key is NOT based on args — it's based on task/execution/step."""
    ctx1 = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        tool_name="s", tool_args={"a": 1},
    )
    ctx2 = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        tool_name="s", tool_args={"a": 2},
    )
    assert ctx1.idempotency_key == ctx2.idempotency_key


@pytest.mark.asyncio
async def test_handler_exception_recorded() -> None:
    async def broken_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("unexpected crash")

    runtime = ToolRuntime(broken_handler)
    ctx = _ctx()
    result = await runtime.invoke(ctx)

    assert result.ok is False
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert result.retryable is False


def test_ledger_rejects_duplicate_key_without_replay() -> None:
    """record_start raises if key already used (even if not replayed)."""
    ledger = ToolExecutionLedger()
    ctx = _ctx()
    ledger.record_start(ctx)
    with pytest.raises(ValueError, match="already used"):
        ledger.record_start(ctx)
