from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
import json
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings
from app.tools import RiskLevel, ToolRegistry


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str
    allowed_tools: frozenset[str]
    headers: dict[str, str]


@dataclass(frozen=True)
class McpBinding:
    qualified_name: str
    server: McpServer
    remote_name: str
    input_schema: dict[str, Any]
    risk: RiskLevel
    side_effecting: bool


class McpGateway:
    """A deny-by-default boundary for remote MCP tools.

    Only explicitly listed tools are discovered and registered. MCP results are
    untrusted data and are size-bounded before entering model context.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.servers = self._parse_servers(settings.mcp_servers_json)
        self.bindings: dict[str, McpBinding] = {}

    @staticmethod
    def _parse_servers(raw: str) -> list[McpServer]:
        payload = json.loads(raw or "[]")
        if not isinstance(payload, list):
            raise ValueError("ASSISTANT_MCP_SERVERS_JSON must be a JSON array")
        servers: list[McpServer] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("Each MCP server config must be an object")
            name = str(item.get("name") or "").strip()
            url = str(item.get("url") or "").strip()
            allowed = item.get("allowed_tools")
            if (
                not name
                or not url
                or not isinstance(allowed, list)
                or not allowed
            ):
                raise ValueError(
                    "MCP server requires name, url and non-empty allowed_tools"
                )
            if not name.replace("-", "").replace("_", "").isalnum():
                raise ValueError(f"Invalid MCP server name: {name}")
            servers.append(
                McpServer(
                    name=name,
                    url=url,
                    allowed_tools=frozenset(str(value) for value in allowed),
                    headers={
                        str(key): str(value)
                        for key, value in dict(item.get("headers") or {}).items()
                    },
                )
            )
        return servers

    async def discover(self, registry: ToolRegistry) -> None:
        for server in self.servers:
            async with self._session(server) as session:
                result = await session.list_tools()
            for tool in result.tools:
                if tool.name not in server.allowed_tools:
                    continue
                qualified = f"mcp.{server.name}.{tool.name}"
                schema = dict(tool.inputSchema or {"type": "object"})
                Draft202012Validator.check_schema(schema)
                annotations = getattr(tool, "annotations", None)
                read_only = bool(
                    getattr(annotations, "readOnlyHint", False)
                )
                destructive = bool(
                    getattr(annotations, "destructiveHint", False)
                )
                risk = (
                    RiskLevel.READ
                    if read_only
                    else (
                        RiskLevel.EXTERNAL_WRITE
                        if destructive
                        else RiskLevel.REVERSIBLE
                    )
                )
                binding = McpBinding(
                    qualified_name=qualified,
                    server=server,
                    remote_name=tool.name,
                    input_schema=schema,
                    risk=risk,
                    side_effecting=not read_only,
                )
                self.bindings[qualified] = binding
                registry.register_mcp_tool(
                    name=qualified,
                    label=f"MCP · {server.name} · {tool.name}",
                    description=(
                        f"通过受控 MCP Server {server.name} 调用 {tool.name}。"
                        f"{tool.description or ''}"
                    )[:1_000],
                    risk=risk,
                    side_effecting=not read_only,
                    handler=self._handler_for(qualified),
                )

    def _handler_for(self, qualified_name: str):
        async def handler(
            *, args: dict[str, Any], timeout_seconds: int, **_: Any
        ) -> dict[str, Any]:
            return await self.call(qualified_name, args)

        return handler

    async def call(self, qualified_name: str, arguments: dict[str, Any]) -> dict:
        try:
            binding = self.bindings[qualified_name]
        except KeyError as exc:
            raise ValueError(f"MCP tool is not registered: {qualified_name}") from exc
        errors = sorted(
            Draft202012Validator(binding.input_schema).iter_errors(arguments),
            key=lambda error: list(error.path),
        )
        if errors:
            raise ValueError(f"MCP arguments failed schema validation: {errors[0].message}")
        async with self._session(binding.server) as session:
            result = await session.call_tool(binding.remote_name, arguments)
        content = [
            item.model_dump(mode="json", exclude_none=True)
            for item in result.content
        ]
        payload = {
            "server": binding.server.name,
            "tool": binding.remote_name,
            "structured_content": result.structuredContent,
            "content": content,
            "is_error": bool(result.isError),
        }
        encoded = json.dumps(payload, ensure_ascii=False)
        if len(encoded) > self.settings.mcp_max_result_chars:
            payload["content"] = [
                {
                    "type": "text",
                    "text": encoded[: self.settings.mcp_max_result_chars]
                    + "\n[结果已按上下文预算截断]",
                }
            ]
            payload["structured_content"] = None
        if payload["is_error"]:
            raise RuntimeError(f"MCP tool returned an error: {payload['content']}")
        return payload

    def public_catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": binding.qualified_name,
                "server": binding.server.name,
                "remote_name": binding.remote_name,
                "input_schema": binding.input_schema,
                "risk": binding.risk.value,
                "side_effecting": binding.side_effecting,
            }
            for binding in sorted(
                self.bindings.values(), key=lambda item: item.qualified_name
            )
        ]

    def _session(self, server: McpServer):
        stack = AsyncExitStack()

        class ManagedSession:
            async def __aenter__(managed_self) -> ClientSession:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(headers=server.headers, timeout=60.0)
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(server.url, http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                return session

            async def __aexit__(managed_self, exc_type, exc, traceback) -> None:
                await stack.aclose()

        return ManagedSession()
