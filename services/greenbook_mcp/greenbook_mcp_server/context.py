"""MCP Tool execution context — injected at tool call time, never from user input."""

from __future__ import annotations

from dataclasses import dataclass

from greenbook_contracts.identity import AuthContext
from greenbook_assistant_core.context import SessionContext
from greenbook_java_client.client import JavaClient


@dataclass
class ToolContext:
    auth: AuthContext
    session: SessionContext
    java: JavaClient
    trace_id: str | None = None
    conversation_id: str | None = None
    agent_run_id: str | None = None
    tool_call_id: str | None = None

    def idempotency_key(self, operation: str) -> str:
        """Stable idempotency key: {conversation}:{run}:{operation}:{tool_call}"""
        return ":".join([
            self.conversation_id or "unknown",
            self.agent_run_id or "unknown",
            operation,
            self.tool_call_id or "unknown",
        ])
