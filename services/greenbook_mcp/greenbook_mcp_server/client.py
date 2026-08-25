"""MCP client facade for the canonical GreenBook business boundary."""

from __future__ import annotations

import base64
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

import httpx
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext

from . import tool_registry
from .protocol import MCP_PROTOCOL_VERSION
from .server import GreenBookMCPServer

_DEFAULT_TRANSPORT_MODE = "mcp"
_SUPPORTED_TRANSPORT_MODES = frozenset({"mcp", "local"})
_SENTINEL = re.compile(r"^=\?base64\?.*\?=$")


def _header_value(value: str) -> str:
    """Encode a non-ASCII/unsafe MCP header value per 2026-07-28."""

    if (
        value
        and not _SENTINEL.match(value)
        and all(0x20 <= ord(char) <= 0x7E for char in value)
        and value == value.strip()
    ):
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?base64?{encoded}?="


def _meta() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": {
            "name": "greenbook-agent-runtime",
            "version": "2.0.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


class GreenBookMCPClient:
    """Preserve the existing execute_tool shape across one explicit boundary.

    ``mcp`` is the production/default transport and routes every active
    ToolContract through the Business MCP endpoint.  ``local`` is an explicit
    test/isolated-development mode.  There is deliberately no automatic
    local fallback after a remote attempt: a write whose response is lost
    must remain ``RESULT_UNKNOWN`` for the existing reconciliation path.

    ``remote_tools`` is retained only as an explicit test allow-list while a
    focused test exercises one provider slice.  Production construction does
    not read a partial remote-tool environment variable; absent an explicit
    test allow-list, all active registry tools are remote.
    """

    def __init__(
        self,
        local_server: GreenBookMCPServer,
        *,
        base_url: str | None = None,
        remote_tools: Iterable[str] | None = None,
        transport_mode: str | None = None,
        runtime_token: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.local_server = local_server
        configured_base_url = (
            base_url
            if base_url is not None
            else os.getenv("GREENBOOK_BUSINESS_MCP_BASE_URL", "")
        )
        self.base_url = configured_base_url.rstrip("/")
        configured_mode = (
            transport_mode
            or os.getenv("GREENBOOK_MCP_TRANSPORT", _DEFAULT_TRANSPORT_MODE)
        ).strip().lower()
        if configured_mode not in _SUPPORTED_TRANSPORT_MODES:
            raise ValueError(
                "GREENBOOK_MCP_TRANSPORT must be one of: "
                + ", ".join(sorted(_SUPPORTED_TRANSPORT_MODES))
            )
        self.transport_mode = configured_mode
        active_tools = frozenset(item.name for item in tool_registry.list_tools())
        self.remote_tools = (
            active_tools
            if remote_tools is None
            else frozenset(str(item) for item in remote_tools)
        )
        self.runtime_token = runtime_token or os.getenv("GREENBOOK_MCP_RUNTIME_TOKEN", "")
        self.timeout_seconds = timeout_seconds or float(
            os.getenv("GREENBOOK_MCP_TIMEOUT_SECONDS", "30")
        )
        self.transport = transport

    @property
    def java(self) -> Any:
        return self.local_server.java

    @property
    def llm(self) -> Any:
        return self.local_server.llm

    @property
    def model(self) -> str:
        return self.local_server.model

    @property
    def uses_local_transport(self) -> bool:
        return self.transport_mode == "local"

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return self.local_server.get_tool_definitions()

    async def execute_tool(
        self,
        tool_name: str,
        *,
        auth: AuthContext,
        session: SessionContext,
        trace_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
        approval_granted: bool = False,
        **kwargs: object,
    ) -> dict[str, Any]:
        try:
            definition = tool_registry.get_tool(tool_name)
        except ValueError:
            if self.uses_local_transport:
                return await self.local_server.execute_tool(
                    tool_name,
                    auth=auth,
                    session=session,
                    trace_id=trace_id,
                    agent_run_id=agent_run_id,
                    tool_call_id=tool_call_id,
                    approval_granted=approval_granted,
                    **kwargs,
                )
            return {
                "ok": False,
                "code": "CONTRACT_MISMATCH",
                "message": f"Tool '{tool_name}' is not in the active MCP catalog",
                "retryable": False,
                "request_sent": False,
                "state": {
                    "phase": "MCP_CONFIGURATION_ERROR",
                    "downstream_called": False,
                    "side_effect_started": False,
                    "safe_to_retry": False,
                },
            }

        if self.uses_local_transport:
            return await self.local_server.execute_tool(
                tool_name,
                auth=auth,
                session=session,
                trace_id=trace_id,
                agent_run_id=agent_run_id,
                tool_call_id=tool_call_id,
                approval_granted=approval_granted,
                **kwargs,
            )

        # A partial allow-list is useful only for an explicit focused test.
        # It must fail closed instead of silently routing the omitted Tool to
        # the in-process server.
        if tool_name not in self.remote_tools:
            return {
                "ok": False,
                "code": "MCP_TOOL_NOT_EXPOSED",
                "message": f"Tool '{tool_name}' is not exposed by this MCP client",
                "retryable": False,
                "request_sent": False,
                "state": {
                    "phase": "MCP_CONFIGURATION_ERROR",
                    "downstream_called": False,
                    "side_effect_started": False,
                    "safe_to_retry": False,
                },
            }

        if not self.base_url:
            return self._transport_failure(
                "MCP_UNAVAILABLE",
                is_write=definition.policy.side_effect.has_side_effect,
                detail="GreenBook Business MCP endpoint is not configured",
            )

        return await self._execute_remote(
            tool_name,
            auth=auth,
            session=session,
            trace_id=trace_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            approval_granted=approval_granted,
            arguments=dict(kwargs),
        )

    def _headers(
        self,
        *,
        method: str,
        auth: AuthContext,
        session: SessionContext,
        name: str | None = None,
        approval_granted: bool = False,
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            "Mcp-Method": method,
            "X-GreenBook-Conversation-ID": session.conversation_id,
            "X-GreenBook-User-ID": auth.user_id,
            "X-GreenBook-Tenant-ID": auth.tenant_id,
            "X-GreenBook-Timezone": session.timezone,
        }
        if name is not None:
            headers["Mcp-Name"] = _header_value(name)
        if auth.raw_access_token:
            headers["Authorization"] = f"Bearer {auth.raw_access_token}"
        if self.runtime_token:
            headers["X-GreenBook-MCP-Runtime-Token"] = self.runtime_token
        if approval_granted:
            headers["X-GreenBook-Approval-Granted"] = "true"
        return headers

    async def list_remote_tools(
        self,
        *,
        auth: AuthContext,
        session: SessionContext,
    ) -> dict[str, Any]:
        """Fetch the protocol catalog for contract/equivalence tests."""

        if self.uses_local_transport:
            return {
                "ok": True,
                "code": "LOCAL_TRANSPORT",
                "tools": self.local_server.get_tool_definitions(),
            }
        if not self.base_url:
            return self._transport_failure(
                "MCP_UNAVAILABLE",
                is_write=False,
                detail="GreenBook Business MCP endpoint is not configured",
            )
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/list",
            "params": {"_meta": _meta()},
        }
        headers = self._headers(
            method="tools/list",
            auth=auth,
            session=session,
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.post(self.base_url, headers=headers, json=payload)
            envelope = response.json()
        except httpx.TimeoutException as exc:
            return self._transport_failure("MCP_TIMEOUT", is_write=False, detail=str(exc))
        except httpx.RequestError as exc:
            return self._transport_failure("MCP_UNAVAILABLE", is_write=False, detail=str(exc))
        except (ValueError, json.JSONDecodeError) as exc:
            return self._protocol_failure(str(exc))
        if response.status_code >= 500:
            return self._transport_failure(
                "MCP_UNAVAILABLE",
                is_write=False,
                detail=f"provider HTTP {response.status_code}",
            )
        if response.status_code in {401, 403}:
            return {
                "ok": False,
                "code": "AUTHENTICATION_REQUIRED" if response.status_code == 401 else "PERMISSION_DENIED",
                "message": "MCP provider rejected the trusted identity",
                "request_sent": False,
            }
        if not isinstance(envelope, dict) or "result" not in envelope:
            return self._protocol_failure("MCP tools/list response did not contain a result")
        result = envelope["result"]
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return self._protocol_failure("MCP tools/list result is not a tool catalog")
        return {"ok": True, "code": "OK", "tools": result["tools"]}

    async def _execute_remote(
        self,
        tool_name: str,
        *,
        auth: AuthContext,
        session: SessionContext,
        trace_id: str | None,
        agent_run_id: str | None,
        tool_call_id: str | None,
        approval_granted: bool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        definition = tool_registry.get_tool(tool_name)
        is_write = definition.policy.side_effect.has_side_effect
        payload = {
            "jsonrpc": "2.0",
            "id": tool_call_id or str(uuid.uuid4()),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
                "_meta": _meta(),
            },
        }
        if trace_id:
            payload["params"]["_meta"]["io.modelcontextprotocol/traceparent"] = trace_id
        headers = self._headers(
            method="tools/call",
            auth=auth,
            session=session,
            name=tool_name,
            approval_granted=approval_granted,
        )
        if trace_id:
            headers["X-GreenBook-Trace-ID"] = trace_id
        if agent_run_id:
            headers["X-GreenBook-Agent-Run-ID"] = agent_run_id
        if tool_call_id:
            headers["X-GreenBook-Tool-Call-ID"] = tool_call_id
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                trust_env=False,
                transport=self.transport,
            ) as client:
                response = await client.post(self.base_url, headers=headers, json=payload)
            envelope = response.json()
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            return self._transport_failure(
                "MCP_TIMEOUT",
                is_write=is_write,
                detail=str(exc),
                response_lost=True,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            return self._transport_failure(
                "MCP_UNAVAILABLE",
                is_write=is_write,
                detail=str(exc),
            )
        except httpx.RequestError as exc:
            return self._transport_failure(
                "MCP_UNAVAILABLE",
                is_write=is_write,
                detail=str(exc),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            return self._transport_failure(
                "MCP_TIMEOUT" if is_write else "CONTRACT_MISMATCH",
                is_write=is_write,
                detail=str(exc),
                response_lost=True,
            )

        if response.status_code in {401, 403}:
            return {
                "ok": False,
                "code": "AUTHENTICATION_REQUIRED" if response.status_code == 401 else "PERMISSION_DENIED",
                "message": "MCP provider rejected the trusted identity",
                "retryable": False,
                "request_sent": False,
                "state": {"phase": "MCP_AUTHORIZATION_REJECTED", "downstream_called": False},
            }
        if response.status_code >= 500:
            return self._transport_failure(
                "MCP_UNAVAILABLE",
                is_write=is_write,
                detail=f"provider HTTP {response.status_code}",
                response_lost=is_write,
            )
        if not isinstance(envelope, Mapping):
            return self._transport_failure(
                "CONTRACT_MISMATCH",
                is_write=is_write,
                detail="MCP response is not a JSON object",
                response_lost=is_write,
            )
        if "error" in envelope:
            return self._protocol_failure(
                str((envelope.get("error") or {}).get("message") or "MCP protocol error"),
                protocol_code=(envelope.get("error") or {}).get("code"),
            )
        result = envelope.get("result")
        if not isinstance(result, Mapping):
            return self._transport_failure(
                "CONTRACT_MISMATCH",
                is_write=is_write,
                detail="MCP tools/call response did not contain a result",
                response_lost=is_write,
            )
        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            return dict(structured)
        content = result.get("content")
        if isinstance(content, list) and content and isinstance(content[0], Mapping):
            try:
                decoded = json.loads(str(content[0].get("text") or ""))
            except (TypeError, ValueError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, Mapping):
                return dict(decoded)
        return self._transport_failure(
            "CONTRACT_MISMATCH",
            is_write=is_write,
            detail="MCP CallToolResult had no structured result",
            response_lost=is_write,
        )

    @staticmethod
    def _protocol_failure(message: str, *, protocol_code: Any = None) -> dict[str, Any]:
        state: dict[str, Any] = {
            "phase": "MCP_PROTOCOL_ERROR",
            "downstream_called": False,
            "side_effect_started": False,
            "safe_to_retry": False,
        }
        if protocol_code is not None:
            state["protocol_code"] = protocol_code
        return {
            "ok": False,
            "code": "CONTRACT_MISMATCH",
            "message": message,
            "user_message": "The business tool protocol is temporarily incompatible.",
            "retryable": False,
            "request_sent": False,
            "state": state,
        }

    @staticmethod
    def _transport_failure(
        code: str,
        *,
        is_write: bool,
        detail: str,
        response_lost: bool = False,
    ) -> dict[str, Any]:
        if is_write and response_lost:
            return {
                "ok": False,
                "code": "RESULT_UNKNOWN",
                "message": f"MCP response lost after a write request: {detail}",
                "user_message": "The operation may have been submitted; status must be reconciled before retrying.",
                "retryable": False,
                "request_sent": None,
                "state": {
                    "phase": "MCP_RESPONSE_LOST",
                    "dependency": "mcp",
                    "downstream_called": None,
                    "side_effect_started": True,
                    "side_effect_state": "POSSIBLE",
                    "safe_to_retry": False,
                },
            }
        return {
            "ok": False,
            "code": code,
            "message": detail,
            "user_message": "The business tool service is temporarily unavailable. No business result was confirmed.",
            "retryable": code in {"MCP_UNAVAILABLE", "MCP_TIMEOUT"},
            "request_sent": bool(response_lost),
            "state": {
                "phase": "MCP_TRANSPORT_FAILURE",
                "dependency": "mcp",
                "downstream_called": response_lost,
                "side_effect_started": False,
                "safe_to_retry": not response_lost,
            },
        }


__all__ = ["GreenBookMCPClient"]
