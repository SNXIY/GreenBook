"""MCP Tool execution context — injected at tool call time, never from user input."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
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
    approval_granted: bool = False
    # Lightweight content generation: the host injects its LLM client so a
    # plain "write an article" request generates the draft directly in the
    # assistant turn (prompt + model + MCP, no separate creator service).
    llm: Any = None
    model: str = ""

    def idempotency_key(self, operation: str, scope: str = "") -> str:
        """Return a retry-stable key scoped to the logical business request.

        Run and tool-call IDs are execution identifiers and change on retry;
        using them here would defeat the Java facade's idempotency contract.
        The optional operation scope separates distinct requests in one
        conversation while keeping the header short and secret-free.
        """
        material = ":".join([
            self.conversation_id or "unknown",
            operation,
            scope,
        ])
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
        return f"greenbook:{operation}:{digest}"
