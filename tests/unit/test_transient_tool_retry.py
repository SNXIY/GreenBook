"""Bounded transient tool retry: safe classification + failure-injected replay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from greenbook_agent_core.execution.runtime_agent_service import (
    _invoke_with_transient_retry,
    _is_transient_tool_retryable,
)
from greenbook_agent_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)


def _result(ok: bool, error_code: str = "", request_sent: bool | None = False) -> SimpleNamespace:
    return SimpleNamespace(
        ok=ok, error_code=error_code, request_sent=request_sent,
        pending=False, error_message="",
    )


@pytest.mark.parametrize("code,expected", [
    ("TIMEOUT", True),
    ("RATE_LIMIT", True),
    ("NETWORK_ERROR", True),
    ("DEPENDENCY_UNAVAILABLE", True),
    # Runtime/handler exceptions are internal failures, not transport
    # failures.  They must not be replayed by this bounded retry helper.
    ("TOOL_EXECUTION_FAILED", False),
    # A schema/argument validation failure is permanent at the tool level: the
    # same args would fail again, so it must NOT be auto-retried (a tool-level
    # retry cannot regenerate content or arguments).
    ("TOOL_ARGUMENT_VALIDATION_FAILED", False),
    ("503", True),
    ("429", True),
    ("PERMISSION_DENIED", False),
    ("DRAFT_NOT_FOUND", False),
    ("VERSION_CONFLICT", False),
    ("BUSINESS_REJECTED", False),
])
def test_transient_classification(code: str, expected: bool) -> None:
    assert _is_transient_tool_retryable(_result(False, code, request_sent=False)) is expected


def test_never_retries_unknown_delivery_boundary() -> None:
    assert _is_transient_tool_retryable(_result(False, "TIMEOUT", request_sent=True)) is False
    assert _is_transient_tool_retryable(_result(False, "TIMEOUT", request_sent=None)) is False


def test_never_retries_success_or_pending() -> None:
    assert _is_transient_tool_retryable(_result(True)) is False
    pending = _result(False, "TIMEOUT", request_sent=False)
    pending.pending = True
    assert _is_transient_tool_retryable(pending) is False


@pytest.mark.asyncio
async def test_transient_failure_is_retried_once_and_recovers() -> None:
    calls = {"n": 0}

    class FakeRuntime:
        async def invoke(self, ctx: ToolInvocationContext):
            calls["n"] += 1
            if calls["n"] == 1:
                return _result(False, "TIMEOUT", request_sent=False)
            return _result(True)

    ctx = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        capability="MANAGE_DRAFT", tool_name="content.update_draft",
    )
    result = await _invoke_with_transient_retry(FakeRuntime(), ctx, backoff_seconds=0.0)
    assert result.ok is True
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried() -> None:
    calls = {"n": 0}

    class FakeRuntime:
        async def invoke(self, ctx: ToolInvocationContext):
            calls["n"] += 1
            return _result(False, "DRAFT_NOT_FOUND", request_sent=False)

    ctx = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        capability="MANAGE_DRAFT", tool_name="content.update_draft",
    )
    result = await _invoke_with_transient_retry(FakeRuntime(), ctx)
    assert result.error_code == "DRAFT_NOT_FOUND"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_two_consecutive_transient_failures_stand() -> None:
    calls = {"n": 0}

    class FakeRuntime:
        async def invoke(self, ctx: ToolInvocationContext):
            calls["n"] += 1
            return _result(False, "TIMEOUT", request_sent=False)

    ctx = ToolInvocationContext.build(
        task_id="t1", execution_id="e1", step_id="s1",
        capability="MANAGE_DRAFT", tool_name="content.update_draft",
    )
    result = await _invoke_with_transient_retry(FakeRuntime(), ctx, backoff_seconds=0.0)
    assert result.error_code == "TIMEOUT"
    assert calls["n"] == 2
