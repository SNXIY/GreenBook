"""ToolInvocationContext — all the metadata for one tool call."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from ...observability.context import TraceContext


class ToolInvocationContext(BaseModel):
    """Complete context for a single MCP tool invocation."""

    invocation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # ── routing ──
    task_id: str = ""
    execution_id: str = ""
    step_id: str = ""
    capability: str = ""

    # ── tool ──
    tool_name: str = ""
    tool_args: dict[str, Any] = {}

    # ── identity ──
    user_id: str = ""
    tenant_id: str = ""

    # ── reliability ──
    idempotency_key: str = ""
    timeout_seconds: float = 60.0

    # ── timing ──
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    trace_context: TraceContext = Field(default_factory=TraceContext)

    @classmethod
    def build(
        cls,
        *,
        task_id: str = "",
        execution_id: str = "",
        step_id: str = "",
        capability: str = "",
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        user_id: str = "",
        timeout_seconds: float = 60.0,
        trace_context: TraceContext | None = None,
    ) -> ToolInvocationContext:
        """Factory that auto-generates a stable idempotency key."""
        args = tool_args or {}
        material = ":".join([
            task_id or "unknown",
            execution_id or "unknown",
            step_id or "unknown",
            tool_name or "unknown",
        ])
        digest = hashlib.sha256(material.encode()).hexdigest()[:32]
        invocation = cls(
            task_id=task_id,
            execution_id=execution_id,
            step_id=step_id,
            capability=capability,
            tool_name=tool_name,
            tool_args=dict(args),
            user_id=user_id,
            timeout_seconds=timeout_seconds,
            idempotency_key=f"invoke:{tool_name}:{digest}",
            trace_context=trace_context or TraceContext(),
        )
        invocation.trace_context = invocation.trace_context.with_updates(
            task_id=task_id,
            step_id=step_id,
            invocation_id=invocation.invocation_id,
            trace_id=(trace_context.trace_id if trace_context is not None else None),
            execution_id=(
                execution_id
                if execution_id
                else (trace_context.execution_id if trace_context is not None else None)
            ),
        )
        return invocation
