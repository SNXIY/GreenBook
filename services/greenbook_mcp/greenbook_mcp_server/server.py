"""GreenBook MCP Server — assembles tools and provides the execution boundary.

Phase 1: In-process tool abstraction (no remote MCP transport required).
Phase 2: Streamable HTTP MCP Server.
"""

from __future__ import annotations

import logging

from greenbook_contracts.identity import AuthContext
from greenbook_assistant_core.context import SessionContext
from greenbook_java_client.client import JavaClient
from greenbook_creator_client.client import CreatorClient

from .context import ToolContext
from . import tool_registry

logger = logging.getLogger(__name__)


class GreenBookMCPServer:
    """In-process MCP server that dispatches tool calls with Pydantic validation."""

    def __init__(
        self,
        java: JavaClient,
        creator: CreatorClient,
    ) -> None:
        self.java = java
        self.creator = creator

    async def execute_tool(
        self,
        tool_name: str,
        *,
        auth: AuthContext,
        session: SessionContext,
        trace_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
        **kwargs: object,
    ) -> dict:
        """Execute a named MCP tool with injected context.

        The tool handler receives ToolContext and keyword args.
        User identity fields are injected, never from kwargs.
        """
        try:
            definition = tool_registry.get_tool(tool_name)
        except ValueError:
            return {
                "ok": False,
                "code": "VALIDATION_ERROR",
                "message": f"Unknown tool: {tool_name}",
                "user_message": f"Tool '{tool_name}' is not available.",
            }

        ctx = ToolContext(
            auth=auth,
            session=session,
            java=self.java,
            creator=self.creator,
            trace_id=trace_id,
            conversation_id=session.conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
        )

        try:
            result = await definition.handler(ctx, **kwargs)
            if hasattr(result, "model_dump"):
                return result.model_dump(mode="json")
            return result
        except Exception as exc:
            logger.exception("Tool '%s' execution failed", tool_name)
            return {
                "ok": False,
                "code": "INTERNAL_ERROR",
                "message": str(exc),
                "user_message": "An unexpected error occurred while processing your request.",
                "retryable": False,
                "request_sent": False,
                "trace_id": trace_id,
            }

    def get_tool_definitions(self) -> list[dict]:
        """Export tool definitions for LLM function-calling."""
        tools = []
        for td in tool_registry.list_tools():
            tools.append({
                "name": td.name,
                "description": td.description,
                "category": td.category,
                "risk": td.risk,
            })
        return tools
