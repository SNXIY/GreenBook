"""GreenBook Business MCP protocol adapter.

The existing ``GreenBookMCPServer`` remains the only execution/runtime
boundary.  This module only translates the active ToolContract registry to and
from the stateless 2026-07-28 Streamable HTTP message shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext

from . import tool_registry
from .server import GreenBookMCPServer

MCP_PROTOCOL_VERSION = "2026-07-28"
MCP_SUPPORTED_PROTOCOL_VERSIONS = (MCP_PROTOCOL_VERSION,)


@dataclass(frozen=True, slots=True)
class TrustedMCPContext:
    """Runtime-injected context; no field is read from tool arguments."""

    auth: AuthContext
    session: SessionContext
    trace_id: str | None = None
    agent_run_id: str | None = None
    tool_call_id: str | None = None
    approval_granted: bool = False


class MCPProtocolError(ValueError):
    """JSON-RPC error that belongs to the MCP protocol envelope."""

    def __init__(self, code: int, message: str, *, data: Any = None) -> None:
        self.code = code
        self.data = data
        super().__init__(message)


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


class GreenBookMCPProtocolAdapter:
    """Expose the existing active registry through MCP tools/list/tools/call."""

    def __init__(self, server: GreenBookMCPServer) -> None:
        self.server = server

    def list_tools(self) -> list[dict[str, Any]]:
        """Return a deterministic, schema-complete active Tool catalog."""

        definitions: list[dict[str, Any]] = []
        for contract in sorted(tool_registry.list_tools(), key=lambda item: item.name):
            policy = contract.policy
            definitions.append(
                {
                    "name": contract.name,
                    "description": contract.description,
                    "inputSchema": contract.input_schema.model_json_schema(),
                    "outputSchema": contract.output_schema.model_json_schema(),
                    "annotations": {
                        "readOnlyHint": not policy.side_effect.has_side_effect,
                        "destructiveHint": policy.side_effect.destructive,
                        "idempotentHint": policy.side_effect.idempotent,
                        "openWorldHint": bool(policy.side_effect.external_systems),
                    },
                }
            )
        return definitions

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        context: TrustedMCPContext,
    ) -> dict[str, Any]:
        """Call one active ToolContract with trusted runtime context."""

        try:
            tool_registry.get_tool(name)
        except ValueError as exc:
            # Finding an unknown tool is a protocol-level invalid params error;
            # it is not a business ToolResult and never reaches a handler.
            raise MCPProtocolError(-32602, f"Unknown tool: {name}") from exc

        result = await self.server.execute_tool(
            name,
            auth=context.auth,
            session=context.session,
            trace_id=context.trace_id,
            agent_run_id=context.agent_run_id,
            tool_call_id=context.tool_call_id,
            approval_granted=context.approval_granted,
            **dict(arguments or {}),
        )
        if not isinstance(result, dict):
            raise MCPProtocolError(
                -32603,
                "GreenBook ToolRuntime returned a non-object result",
            )
        return result

    @staticmethod
    def tool_result(result: dict[str, Any]) -> dict[str, Any]:
        """Project the existing typed result into MCP CallToolResult."""

        payload = dict(result)
        observability = payload.pop("_greenbook_mcp_observability", None)
        projected = {
            "resultType": "complete",
            "content": [{"type": "text", "text": _json_text(payload)}],
            "structuredContent": payload,
            "isError": not bool(payload.get("ok")),
        }
        if isinstance(observability, dict):
            projected["_meta"] = {
                "greenbook.performance": observability,
            }
        return projected


__all__ = [
    "GreenBookMCPProtocolAdapter",
    "MCPProtocolError",
    "MCP_PROTOCOL_VERSION",
    "MCP_SUPPORTED_PROTOCOL_VERSIONS",
    "TrustedMCPContext",
]
