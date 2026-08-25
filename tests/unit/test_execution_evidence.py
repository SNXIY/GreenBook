from __future__ import annotations

import asyncio
from typing import Any

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.execution.evidence import ExecutionEvidence
from greenbook_agent_core.execution.invocation import ExecutionResult
from greenbook_agent_core.execution.runtime.invocation_context import (
    ToolInvocationContext,
)
from greenbook_agent_core.execution.runtime.tool_runtime import ToolRuntime
from greenbook_agent_core.planning.contracts import PlanStep
from greenbook_contracts import SideEffectState, ToolResult, normalize_external_failure


def _evidence(**overrides: Any) -> ExecutionEvidence:
    values: dict[str, Any] = {
        "execution_id": "execution-1",
        "step_id": "step-1",
        "invocation_id": "invocation-1",
        "operation_id": "operation-1",
        "request_hash": "request-hash",
        "request_time": "2026-08-10T00:00:00+00:00",
        "runtime_idempotency_key": "runtime-key",
        "external_idempotency_key": "external-key",
        "trace_id": "trace-1",
    }
    values.update(overrides)
    return ExecutionEvidence(**values)


def _ctx() -> ToolInvocationContext:
    return ToolInvocationContext.build(
        task_id="task-1",
        execution_id="execution-1",
        step_id="step-1",
        capability="SEARCH_COMMUNITY",
        tool_name="community.search_public_posts",
        tool_args={"query": "Java"},
        timeout_seconds=0.05,
    )


@pytest.mark.asyncio
async def test_pre_execution_evidence_preserves_false_and_none_side_effect() -> None:
    evidence = _evidence(
        request_sent=False,
        side_effect_state=SideEffectState.NONE,
        error_code="INVALID_ARGUMENT",
        phase="PRE_EXECUTION_VALIDATION_FAILED",
    )

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "INVALID_ARGUMENT",
            "request_sent": False,
            "state": {"side_effect_state": "NONE"},
            "evidence": evidence.model_dump(mode="json"),
        }

    result = await CapabilityExecutor(
        CapabilityRegistry(), handler,
    ).execute_step(PlanStep(capability="SEARCH_COMMUNITY", ordinal=1, tool_name="community.search_public_posts"))

    assert result.evidence is not None
    assert result.evidence.request_sent is False
    assert result.evidence.side_effect_state is SideEffectState.NONE
    assert result.external_failure is not None
    assert result.external_failure.request_sent is False
    assert result.external_failure.side_effect_state is SideEffectState.NONE


@pytest.mark.asyncio
async def test_runtime_timeout_keeps_unknown_delivery_evidence() -> None:
    async def slow_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {"ok": True, "data": {}}

    runtime = ToolRuntime(slow_handler)
    result = await runtime.invoke(_ctx())

    assert result.error_code == "TIMEOUT"
    assert result.request_sent is None
    assert result.evidence is not None
    assert result.evidence.request_sent is None
    assert result.evidence.side_effect_state is SideEffectState.UNKNOWN
    assert result.evidence.phase == "TOOL_RUNTIME_TIMEOUT"

    ledger_entry = runtime.ledger.find_by_id(result.invocation_id)
    assert ledger_entry is not None
    assert ledger_entry.evidence is not None
    assert ledger_entry.evidence.request_sent is None
    assert ledger_entry.evidence.side_effect_state is SideEffectState.UNKNOWN


@pytest.mark.asyncio
async def test_handler_exception_is_internal_without_reconciliation_evidence() -> None:
    async def broken_handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("local runtime handler failure")

    runtime = ToolRuntime(broken_handler)
    result = await runtime.invoke(_ctx())

    assert result.error_code == "INTERNAL_ERROR"
    assert result.request_sent is False
    assert result.evidence is not None
    assert result.evidence.request_sent is False
    assert result.evidence.side_effect_state is SideEffectState.NOT_STARTED
    assert result.evidence.raw_error_type == "RuntimeError"


@pytest.mark.asyncio
async def test_external_failure_evidence_survives_normalization() -> None:
    evidence = _evidence(
        request_sent=True,
        side_effect_state=SideEffectState.POSSIBLE,
        receipt_id="receipt-1",
        external_operation_id="external-operation-1",
        error_code="DEPENDENCY_UNAVAILABLE",
        phase="DOWNSTREAM_RESPONSE_READ",
    )

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "code": "DEPENDENCY_UNAVAILABLE",
            "retryable": True,
            "request_sent": True,
            "evidence": evidence.model_dump(mode="json"),
        }

    result = await CapabilityExecutor(
        CapabilityRegistry(), handler,
    ).execute_step(PlanStep(capability="SEARCH_COMMUNITY", ordinal=1, tool_name="community.search_public_posts"))

    assert result.evidence is not None
    assert result.evidence.request_sent is True
    assert result.evidence.side_effect_state is SideEffectState.POSSIBLE
    assert result.evidence.receipt_id == "receipt-1"
    assert result.evidence.external_operation_id == "external-operation-1"
    assert result.external_failure is not None
    assert result.external_failure.receipt_id == "receipt-1"
    assert result.external_failure.evidence is not None
    assert result.external_failure.evidence["operation_id"] == "operation-1"

    normalized = normalize_external_failure(
        ToolResult(
            ok=False,
            code="DEPENDENCY_UNAVAILABLE",
            retryable=True,
            request_sent=False,
        ),
        evidence=evidence,
    )
    assert normalized.request_sent is True
    assert normalized.side_effect_state is SideEffectState.POSSIBLE
    assert normalized.receipt_id == "receipt-1"


def test_success_evidence_is_carried_by_execution_result() -> None:
    evidence = _evidence(
        request_sent=True,
        side_effect_state=SideEffectState.NONE,
        receipt_id="receipt-success",
        operation_id="operation-success",
    )

    result = ExecutionResult.success(
        capability="SEARCH_COMMUNITY",
        tool_name="community.search_public_posts",
        tool_result={
            "ok": True,
            "data": {"items": []},
            "evidence": evidence.model_dump(mode="json"),
        },
    )

    assert result.evidence is not None
    assert result.evidence.operation_id == "operation-success"
    assert result.evidence.receipt_id == "receipt-success"
    assert result.evidence.runtime_idempotency_key == "runtime-key"
    assert result.evidence.external_idempotency_key == "external-key"
