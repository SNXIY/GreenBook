from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any, TypeVar
from urllib.parse import urlparse

from pydantic import BaseModel

from app.core.config import Settings
from app.creator.tools.errors import (
    CreatorToolExecutionError,
    CreatorToolNotFoundError,
    CreatorToolValidationError,
)

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client
except ImportError as exc:  # pragma: no cover
    raise RuntimeError(
        "Install the bounded MCP v1 dependency from pyproject.toml and uv.lock"
    ) from exc


ResultT = TypeVar("ResultT", bound=BaseModel)


class MindFlowCreatorMcpClient:
    """Typed client with tool allowlisting and explicit transport lifecycle."""

    def __init__(
        self,
        settings: Settings,
        *,
        allowed_tools: frozenset[str] | None = None,
    ) -> None:
        self._settings = settings
        configured = frozenset(_csv(settings.creator_mcp_allowed_tools))
        self._allowed_tools = allowed_tools if allowed_tools is not None else configured

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
        output_model: type[ResultT],
    ) -> ResultT:
        if self._allowed_tools and name not in self._allowed_tools:
            raise CreatorToolNotFoundError(
                f"Tool {name} is not in the client allowlist"
            )
        async with self.session() as session:
            advertised = {tool.name for tool in (await session.list_tools()).tools}
            if name not in advertised:
                raise CreatorToolNotFoundError(
                    f"MCP server did not advertise tool {name}"
                )
            result = await session.call_tool(name, arguments=arguments)
        if getattr(result, "isError", False):
            raise CreatorToolExecutionError(_content_message(result))
        structured = getattr(result, "structuredContent", None)
        if structured is None:
            structured = _content_json(result)
        try:
            return output_model.model_validate(structured)
        except Exception as exc:
            raise CreatorToolValidationError(
                f"MCP result for {name} did not match its output schema"
            ) from exc

    @asynccontextmanager
    async def session(self) -> AsyncIterator[ClientSession]:
        transport = self._settings.creator_mcp_transport.strip().lower()
        if transport == "stdio":
            env = _stdio_env(self._settings)
            root = str(self._settings.project_root)
            existing = env.get("PYTHONPATH")
            env["PYTHONPATH"] = (
                root if not existing else f"{root}{os.pathsep}{existing}"
            )
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "app.mcp_tools.creator_server"],
                cwd=root,
                env=env,
                encoding="utf-8",
            )
            async with stdio_client(parameters) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    yield session
            return
        if transport != "streamable-http":
            raise ValueError(
                "CREATOR_MCP_TRANSPORT must be 'stdio' or 'streamable-http'"
            )
        url = self._settings.creator_mcp_resource_server_url.rstrip("/") + "/mcp"
        _validate_http_url(url)
        headers = {"Authorization": f"Bearer {self._settings.creator_mcp_bearer_token}"}
        timeout = timedelta(seconds=self._settings.creator_tool_timeout_seconds)
        async with streamablehttp_client(
            url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=timeout,
        ) as (read_stream, write_stream, _), ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _validate_http_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
        return
    raise ValueError("Remote MCP Streamable HTTP requires HTTPS")


def _content_message(result: Any) -> str:
    messages = [
        str(getattr(item, "text", item))
        for item in (getattr(result, "content", None) or ())
    ]
    return "\n".join(messages) or "MCP tool returned an error"


def _content_json(result: Any) -> dict[str, Any]:
    for item in getattr(result, "content", None) or ():
        text = getattr(item, "text", None)
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise CreatorToolValidationError("MCP result did not contain structured data")


def _csv(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(item.strip() for item in value.split(",") if item.strip())
    )


def _stdio_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    values = {
        "GREENBOOK_CREATOR_DATABASE_URL": settings.creator_database_url,
        "GREENBOOK_CREATOR_DATABASE_ECHO": _boolean(settings.creator_database_echo),
        "GREENBOOK_CREATOR_RETRIEVAL_ENABLED": _boolean(settings.creator_retrieval_enabled),
        "GREENBOOK_CREATOR_RETRIEVAL_SQL_ENABLED": _boolean(
            settings.creator_retrieval_sql_enabled
        ),
        "GREENBOOK_CREATOR_RETRIEVAL_QDRANT_ENABLED": _boolean(
            settings.creator_retrieval_qdrant_enabled
        ),
        "GREENBOOK_CREATOR_COMMUNITY_PROVIDER": settings.creator_community_provider,
        "GREENBOOK_CREATOR_COMMUNITY_JAVA_BASE_URL": (settings.creator_community_java_base_url),
        "GREENBOOK_CREATOR_COMMUNITY_JAVA_SHARED_SECRET": (
            settings.creator_community_java_shared_secret
        ),
        "GREENBOOK_CREATOR_COMMUNITY_JAVA_SERVICE_NAME": (
            settings.creator_community_java_service_name
        ),
        "GREENBOOK_CREATOR_COMMUNITY_JAVA_TENANT_ID": (settings.creator_community_java_tenant_id),
        "GREENBOOK_CREATOR_MCP_TRANSPORT": "stdio",
        "GREENBOOK_CREATOR_MCP_TENANT_ID": settings.creator_mcp_tenant_id,
        "GREENBOOK_CREATOR_MCP_CREATOR_ID": settings.creator_mcp_creator_id,
        "GREENBOOK_CREATOR_MCP_ACTOR_ID": settings.creator_mcp_actor_id,
        "GREENBOOK_CREATOR_MCP_ROLES": settings.creator_mcp_roles,
        "GREENBOOK_CREATOR_MCP_ALLOWED_TOOLS": settings.creator_mcp_allowed_tools,
    }
    env.update(values)
    return env


def _boolean(value: bool) -> str:
    return "true" if value else "false"
