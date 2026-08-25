"""Standalone GreenBook Business MCP Streamable HTTP provider."""

from __future__ import annotations

import base64
import hmac
import os
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from greenbook_agent_core.capability.registry import get_capability_registry
from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.conversation import ConversationNotFoundError, ConversationService
from greenbook_contracts.identity import AuthContext
from greenbook_java_client.client import JavaClient
from greenbook_security.auth_context import AuthContextResolver
from openai import AsyncOpenAI

from . import tool_registry
from .protocol import (
    MCP_PROTOCOL_VERSION,
    GreenBookMCPProtocolAdapter,
    MCPProtocolError,
    TrustedMCPContext,
)
from .server import GreenBookMCPServer


def _rpc_error(
    request_id: Any,
    code: int,
    message: str,
    *,
    status_code: int = 400,
    data: Any = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(
        status_code=status_code,
        content={"jsonrpc": "2.0", "id": request_id, "error": error},
    )


def _decode_header_value(value: str) -> str:
    if value.startswith("=?base64?") and value.endswith("?="):
        encoded = value[len("=?base64?") : -2]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise MCPProtocolError(-32020, "Malformed encoded MCP header") from exc
    if any(ord(char) < 0x20 or ord(char) > 0x7E for char in value):
        raise MCPProtocolError(-32020, "MCP header contains invalid characters")
    return value


def _allowed_origins() -> set[str]:
    raw = os.getenv(
        "GREENBOOK_MCP_ALLOWED_ORIGINS",
        "http://127.0.0.1:8094,http://localhost:8094,http://127.0.0.1:5173,http://localhost:5173",
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def _auth_from_headers(auth: AuthContext, request: Request) -> AuthContext:
    """Bind trusted runtime identity headers to a verified bearer token.

    A worker may present its service JWT while executing a durable operation
    for the user recorded in the queue payload.  The identity headers are
    accepted only together with the internal runtime token; a normal user
    request must match the verified JWT identity exactly.
    """

    user_id = str(request.headers.get("X-GreenBook-User-ID") or auth.user_id)
    tenant_id = str(request.headers.get("X-GreenBook-Tenant-ID") or auth.tenant_id)
    expected_internal = str(getattr(request.app.state, "internal_token", "") or "")
    supplied_internal = str(
        request.headers.get("X-GreenBook-MCP-Runtime-Token") or ""
    )
    matches_verified = user_id == auth.user_id and tenant_id == auth.tenant_id
    trusted_service_context = bool(
        expected_internal
        and hmac.compare_digest(expected_internal, supplied_internal)
    )
    if not matches_verified and not trusted_service_context:
        raise MCPProtocolError(
            -32003,
            "Trusted identity does not match the verified authorization",
        )
    if user_id != auth.user_id or tenant_id != auth.tenant_id:
        return auth.model_copy(update={"user_id": user_id, "tenant_id": tenant_id})
    return auth


def _trusted_runtime_token_matches(request: Request) -> bool:
    expected = str(getattr(request.app.state, "internal_token", "") or "")
    supplied = str(request.headers.get("X-GreenBook-MCP-Runtime-Token") or "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _runtime_write_boundary_failure() -> dict[str, Any]:
    return {
        "ok": False,
        "code": "PERMISSION_DENIED",
        "message": "Write tools are available only through the trusted durable runtime boundary.",
        "user_message": "This write must be submitted through the durable runtime.",
        "retryable": False,
        "request_sent": False,
        "state": {
            "phase": "MCP_RUNTIME_BOUNDARY_REJECTED",
            "downstream_called": False,
            "side_effect_started": False,
            "safe_to_retry": False,
        },
    }


async def _resolve_auth(request: Request) -> AuthContext:
    existing = getattr(request.state, "auth_context", None)
    if isinstance(existing, AuthContext):
        return _auth_from_headers(existing, request)
    resolver = getattr(request.app.state, "auth_resolver", None)
    if resolver is None:
        raise MCPProtocolError(-32003, "MCP authorization is not configured")
    authorization = request.headers.get("Authorization")
    try:
        auth = await resolver(request, authorization)
    except TypeError:
        auth = await resolver(request)
    if not isinstance(auth, AuthContext):
        raise MCPProtocolError(-32003, "MCP authorization returned no trusted identity")
    return _auth_from_headers(auth, request)


async def _load_session(request: Request, auth: AuthContext) -> SessionContext:
    conversation_id = str(
        request.headers.get("X-GreenBook-Conversation-ID") or ""
    ).strip()
    if not conversation_id:
        raise MCPProtocolError(-32602, "X-GreenBook-Conversation-ID is required")
    service = getattr(request.app.state, "conversation_service", None)
    if service is None:
        raise MCPProtocolError(-32003, "Conversation ownership service is unavailable")
    try:
        snapshot = await service.load(
            conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
    except ConversationNotFoundError as exc:
        raise MCPProtocolError(-32003, "Conversation is outside the authorized scope") from exc
    session = snapshot.session
    if session.user_id != auth.user_id or session.tenant_id != auth.tenant_id:
        raise MCPProtocolError(-32003, "Conversation ownership verification failed")
    return session


async def _build_runtime(app: FastAPI) -> tuple[GreenBookMCPServer, ConversationService, Any, Any]:
    java = JavaClient.from_env(
        base_url=os.getenv("GREENBOOK_JAVA_BASE_URL", "http://127.0.0.1:8080")
    )
    llm = None
    llm_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    if llm_key:
        llm = AsyncOpenAI(
            api_key=llm_key,
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )
    server = GreenBookMCPServer(
        java=java,
        capability_registry=get_capability_registry(),
        llm=llm,
        model=os.getenv("LLM_MODEL", "deepseek-v4-flash"),
    )
    conversations = ConversationService()
    await conversations.ensure_storage()
    resolver = AuthContextResolver(
        jwks_url=os.getenv(
            "GREENBOOK_AGENT_IDENTITY_JWKS_URL",
            "http://127.0.0.1:8080/.well-known/jwks.json",
        ),
        issuer=os.getenv("GREENBOOK_AGENT_IDENTITY_ISSUER", "http://127.0.0.1:8080"),
        audience=os.getenv("GREENBOOK_AGENT_IDENTITY_AUDIENCE", "greenbook-agent-runtime"),
    )
    return server, conversations, resolver, java


def create_app(
    *,
    server: GreenBookMCPServer | None = None,
    conversation_service: Any | None = None,
    auth_resolver: Any | None = None,
    internal_token: str | None = None,
) -> FastAPI:
    injected = server is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        java = None
        llm = None
        if not injected:
            built_server, conversations, resolver, java = await _build_runtime(app)
            app.state.mcp_server = built_server
            app.state.conversation_service = conversations
            app.state.auth_resolver = resolver
            llm = built_server.llm
        yield
        if not injected:
            if llm is not None:
                await llm.close()
            if java is not None:
                await java.close()

    app = FastAPI(title="GreenBook Business MCP", version="2.0.0", lifespan=lifespan)
    app.state.internal_token = (
        internal_token
        if internal_token is not None
        else os.getenv("GREENBOOK_MCP_RUNTIME_TOKEN", "")
    )
    if server is not None:
        app.state.mcp_server = server
    if conversation_service is not None:
        app.state.conversation_service = conversation_service
    if auth_resolver is not None:
        app.state.auth_resolver = auth_resolver
    app.state.mcp_protocol = GreenBookMCPProtocolAdapter(
        getattr(app.state, "mcp_server", None)
    ) if getattr(app.state, "mcp_server", None) is not None else None

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        protocol = getattr(request.app.state, "mcp_protocol", None)
        if protocol is None and getattr(request.app.state, "mcp_server", None) is not None:
            protocol = GreenBookMCPProtocolAdapter(request.app.state.mcp_server)
            request.app.state.mcp_protocol = protocol
        return {
            "status": "UP" if protocol is not None else "DEGRADED",
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "activeToolCount": len(protocol.list_tools()) if protocol is not None else 0,
        }

    @app.post("/mcp")
    async def mcp_endpoint(request: Request) -> JSONResponse:
        origin = request.headers.get("Origin")
        if origin and origin not in _allowed_origins():
            return _rpc_error(None, -32003, "Origin is not allowed", status_code=403)

        try:
            body = await request.json()
        except Exception:
            return _rpc_error(None, -32700, "Invalid JSON")
        request_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
            return _rpc_error(request_id, -32600, "Invalid JSON-RPC request")
        method = body.get("method")
        params = body.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return _rpc_error(request_id, -32600, "Invalid JSON-RPC request")
        if request_id is None:
            return _rpc_error(None, -32600, "MCP Streamable HTTP requires a request id")

        protocol_version = request.headers.get("MCP-Protocol-Version")
        method_header = request.headers.get("Mcp-Method")
        accept = request.headers.get("Accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return _rpc_error(request_id, -32600, "MCP Accept must include application/json and text/event-stream")
        meta = params.get("_meta")
        body_version = meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
        if protocol_version != MCP_PROTOCOL_VERSION or body_version != protocol_version:
            return _rpc_error(
                request_id,
                -32020,
                "MCP protocol version header does not match request metadata",
                data={"supported": [MCP_PROTOCOL_VERSION]},
            )
        if method_header != method:
            return _rpc_error(request_id, -32020, "Mcp-Method does not match the JSON-RPC method")

        name: str | None = None
        if method == "tools/call":
            name_value = params.get("name")
            if not isinstance(name_value, str) or not name_value:
                return _rpc_error(request_id, -32602, "tools/call requires params.name")
            try:
                name_header = _decode_header_value(request.headers.get("Mcp-Name") or "")
            except MCPProtocolError as exc:
                return _rpc_error(request_id, exc.code, str(exc))
            if name_header != name_value:
                return _rpc_error(request_id, -32020, "Mcp-Name does not match params.name")
            name = name_value

        if method != "tools/list" and method != "tools/call":
            return _rpc_error(request_id, -32601, f"Method not found: {method}", status_code=404)

        protocol = getattr(request.app.state, "mcp_protocol", None)
        if protocol is None and getattr(request.app.state, "mcp_server", None) is not None:
            protocol = GreenBookMCPProtocolAdapter(request.app.state.mcp_server)
            request.app.state.mcp_protocol = protocol
        if protocol is None:
            return _rpc_error(request_id, -32001, "MCP provider is unavailable", status_code=503)

        try:
            auth = await _resolve_auth(request)
            if method == "tools/list":
                result = {
                    "resultType": "complete",
                    "tools": protocol.list_tools(),
                    "ttlMs": 300_000,
                    "cacheScope": "private",
                }
            else:
                session = await _load_session(request, auth)
                arguments = params.get("arguments")
                if arguments is not None and not isinstance(arguments, dict):
                    return _rpc_error(request_id, -32602, "tools/call arguments must be an object")
                try:
                    definition = tool_registry.get_tool(name or "")
                except ValueError:
                    definition = None
                if (
                    definition is not None
                    and definition.policy.side_effect.has_side_effect
                    and (
                        not _trusted_runtime_token_matches(request)
                        or not request.headers.get("X-GreenBook-Agent-Run-ID")
                    )
                ):
                    result = protocol.tool_result(_runtime_write_boundary_failure())
                    return JSONResponse(
                        status_code=200,
                        content={"jsonrpc": "2.0", "id": request_id, "result": result},
                    )
                context = TrustedMCPContext(
                    auth=auth,
                    session=session,
                    trace_id=request.headers.get("X-GreenBook-Trace-ID"),
                    agent_run_id=request.headers.get("X-GreenBook-Agent-Run-ID"),
                    tool_call_id=request.headers.get("X-GreenBook-Tool-Call-ID") or str(request_id),
                    approval_granted=(
                        request.headers.get("X-GreenBook-Approval-Granted", "").lower() == "true"
                    ),
                )
                try:
                    raw_result = await protocol.call_tool(name or "", arguments, context=context)
                except MCPProtocolError as exc:
                    return _rpc_error(request_id, exc.code, str(exc), data=exc.data)
                result = protocol.tool_result(raw_result)
                service = getattr(request.app.state, "conversation_service", None)
                if service is not None:
                    with suppress(Exception):
                        await service.save_session(session)
            return JSONResponse(
                status_code=200,
                content={"jsonrpc": "2.0", "id": request_id, "result": result},
            )
        except MCPProtocolError as exc:
            status_code = 403 if exc.code == -32003 else 400
            return _rpc_error(request_id, exc.code, str(exc), status_code=status_code, data=exc.data)
        except Exception:
            return _rpc_error(request_id, -32603, "MCP provider failed before producing a result", status_code=500)

    return app


__all__ = ["create_app"]
