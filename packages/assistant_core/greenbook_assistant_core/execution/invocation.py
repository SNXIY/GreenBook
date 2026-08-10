"""Tool invocation and execution result models — Phase 4.0."""

from __future__ import annotations

from typing import Any

from greenbook_contracts import ExternalAgentFailure
from pydantic import BaseModel

from greenbook_assistant_core.execution.models import ArtifactHandle


class ToolInvocation(BaseModel):
    """A concrete call to an MCP tool, ready for execution."""

    tool_name: str = ""                  # MCP dot-format name
    tool_args: dict[str, Any] = {}       # keyword arguments for the tool handler
    capability: str = ""                 # which capability this invocation serves
    approval_required: bool = False
    side_effect: bool = False
    idempotency_scope: str = ""          # for building stable idempotency keys


class ExecutionResult(BaseModel):
    """Result of executing one capability step."""

    ok: bool = False
    capability: str = ""
    tool_name: str = ""
    tool_result: dict[str, Any] = {}

    # ── error ──
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False

    # ── artifact ──
    artifact: ArtifactHandle | None = None

    # ── control flow ──
    approval_required: bool = False
    request_sent: bool | None = False    # did the request reach the downstream?
    pending: bool = False                # long-running tool acknowledged work
    async_task_id: str = ""
    # Transient, lossless failure fact for the Worker decision boundary. It
    # is deliberately not part of Execution/StepExecution persistence.
    external_failure: ExternalAgentFailure | None = None

    @classmethod
    def success(
        cls,
        capability: str,
        tool_name: str,
        tool_result: dict[str, Any],
        artifact: ArtifactHandle | None = None,
    ) -> ExecutionResult:
        return cls(
            ok=True,
            capability=capability,
            tool_name=tool_name,
            tool_result=tool_result,
            artifact=artifact,
        )

    @classmethod
    def missing_tool(cls, capability: str) -> ExecutionResult:
        return cls(
            capability=capability,
            error_code="MISSING_TOOL",
            error_message=f"Capability '{capability}' has no tool mapping",
            retryable=False,
        )

    @classmethod
    def unknown_capability(cls, capability: str) -> ExecutionResult:
        return cls(
            capability=capability,
            error_code="UNKNOWN_CAPABILITY",
            error_message=f"Capability '{capability}' is not registered",
            retryable=False,
        )

    @classmethod
    def approval_required_result(cls, capability: str, tool_name: str) -> ExecutionResult:
        return cls(
            capability=capability,
            tool_name=tool_name,
            error_code="APPROVAL_REQUIRED",
            error_message=f"Capability '{capability}' requires user approval",
            approval_required=True,
            retryable=False,
            request_sent=False,
        )

    @classmethod
    def from_tool_error(
        cls,
        capability: str,
        tool_name: str,
        error_code: str,
        error_message: str,
        retryable: bool = False,
        request_sent: bool | None = False,
        tool_result: dict[str, Any] | None = None,
        external_failure: ExternalAgentFailure | None = None,
    ) -> ExecutionResult:
        return cls(
            capability=capability,
            tool_name=tool_name,
            error_code=error_code,
            error_message=error_message,
            retryable=retryable,
            request_sent=request_sent,
            tool_result=tool_result or {},
            external_failure=external_failure,
        )

    @classmethod
    def pending_result(
        cls,
        capability: str,
        tool_name: str,
        tool_result: dict[str, Any],
        task_id: str,
    ) -> ExecutionResult:
        return cls(
            capability=capability,
            tool_name=tool_name,
            tool_result=tool_result,
            pending=True,
            async_task_id=task_id,
            request_sent=True,
        )
