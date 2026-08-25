from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_mcp_server.client import GreenBookMCPClient
from greenbook_mcp_server.http_app import create_app
from greenbook_mcp_server.protocol import (
    MCP_PROTOCOL_VERSION,
    GreenBookMCPProtocolAdapter,
    MCPProtocolError,
    TrustedMCPContext,
)
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_mcp_server.tool_registry import list_tools

AUTH = AuthContext(
    user_id="user-1",
    tenant_id="tenant-1",
    raw_access_token="token-1",
)
SESSION = SessionContext(
    conversation_id="conversation-1",
    user_id="user-1",
    tenant_id="tenant-1",
)


class FakeMCPServer:
    java = object()
    llm = None
    model = "test"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_tool(self, tool_name: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"tool_name": tool_name, **kwargs})
        return {
            "ok": True,
            "code": "OK",
            "data": {"post_id": kwargs.get("post_id", "post-1")},
            "request_sent": False,
        }


@dataclass
class _Snapshot:
    session: SessionContext


class FakeConversationService:
    def __init__(self, session: SessionContext = SESSION) -> None:
        self.session = session
        self.saved: list[SessionContext] = []

    async def load(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> _Snapshot:
        if (
            conversation_id != self.session.conversation_id
            or user_id != self.session.user_id
            or tenant_id != self.session.tenant_id
        ):
            from greenbook_agent_core.conversation import ConversationNotFoundError

            raise ConversationNotFoundError(conversation_id)
        return _Snapshot(self.session)

    async def save_session(self, session: SessionContext) -> None:
        self.saved.append(session)


class FakeAuthResolver:
    async def __call__(self, _request: Any, _authorization: str | None = None) -> AuthContext:
        return AUTH


def _meta() -> dict[str, Any]:
    return {"io.modelcontextprotocol/protocolVersion": MCP_PROTOCOL_VERSION}


def _headers(
    method: str,
    *,
    name: str | None = None,
    runtime_token: str | None = None,
    agent_run_id: str | None = None,
    user_id: str = "user-1",
) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Authorization": "Bearer token-1",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        "Mcp-Method": method,
        "X-GreenBook-Conversation-ID": SESSION.conversation_id,
        "X-GreenBook-User-ID": user_id,
        "X-GreenBook-Tenant-ID": SESSION.tenant_id,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    if runtime_token is not None:
        headers["X-GreenBook-MCP-Runtime-Token"] = runtime_token
    if agent_run_id is not None:
        headers["X-GreenBook-Agent-Run-ID"] = agent_run_id
    return headers


def _call_payload(
    name: str,
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "call-1",
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": arguments or {},
            "_meta": _meta(),
        },
    }


def test_active_registry_is_scoped_and_protocol_catalog_is_deterministic() -> None:
    names = [tool.name for tool in list_tools()]
    assert len(names) == 14
    assert names == list(dict.fromkeys(names))
    assert not {
        "interaction.list_comments",
        "interaction.send_reply",
        "analytics.get_post_performance",
        "analytics.get_account_summary",
    }.intersection(names)

    adapter = GreenBookMCPProtocolAdapter(FakeMCPServer())
    catalog = adapter.list_tools()
    assert [item["name"] for item in catalog] == sorted(names)
    assert all(item["inputSchema"]["type"] == "object" for item in catalog)
    assert all("outputSchema" in item for item in catalog)


def test_transport_mode_rejects_an_implicit_or_unknown_route() -> None:
    with pytest.raises(ValueError, match="GREENBOOK_MCP_TRANSPORT"):
        GreenBookMCPClient(FakeMCPServer(), transport_mode="auto")


@pytest.mark.asyncio
async def test_protocol_call_preserves_trusted_context_and_typed_result() -> None:
    server = FakeMCPServer()
    adapter = GreenBookMCPProtocolAdapter(server)

    result = await adapter.call_tool(
        "community.get_post",
        {"post_id": "post-42"},
        context=TrustedMCPContext(
            auth=AUTH,
            session=SESSION,
            trace_id="trace-1",
            agent_run_id="run-1",
            tool_call_id="tool-call-1",
        ),
    )

    assert result["ok"] is True
    assert server.calls[0]["auth"] == AUTH
    assert server.calls[0]["session"] == SESSION
    assert server.calls[0]["trace_id"] == "trace-1"
    assert server.calls[0]["agent_run_id"] == "run-1"
    assert "user_id" not in server.calls[0]
    projected = adapter.tool_result(result)
    assert projected["resultType"] == "complete"
    assert projected["isError"] is False
    assert projected["structuredContent"] == result


@pytest.mark.asyncio
async def test_http_tools_list_and_tools_call_contract() -> None:
    server = FakeMCPServer()
    app = create_app(
        server=server,
        conversation_service=FakeConversationService(),
        auth_resolver=FakeAuthResolver(),
        internal_token="runtime-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcp.test",
    ) as client:
        listed = await client.post(
            "/mcp",
            headers=_headers("tools/list"),
            json={"jsonrpc": "2.0", "id": "list-1", "method": "tools/list", "params": {"_meta": _meta()}},
        )
        assert listed.status_code == 200
        assert listed.json()["result"]["resultType"] == "complete"
        assert len(listed.json()["result"]["tools"]) == 14

        called = await client.post(
            "/mcp",
            headers=_headers("tools/call", name="community.get_post"),
            json=_call_payload("community.get_post", {"post_id": "post-42"}),
        )
        assert called.status_code == 200
        assert called.json()["result"]["structuredContent"]["data"] == {"post_id": "post-42"}
        assert server.calls[0]["auth"].user_id == "user-1"


@pytest.mark.asyncio
async def test_http_rejects_protocol_mismatch_and_identity_mismatch() -> None:
    server = FakeMCPServer()
    app = create_app(
        server=server,
        conversation_service=FakeConversationService(),
        auth_resolver=FakeAuthResolver(),
        internal_token="runtime-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcp.test",
    ) as client:
        bad_header = _headers("tools/call", name="community.get_post")
        bad_header["Mcp-Name"] = "content.get_draft"
        response = await client.post(
            "/mcp",
            headers=bad_header,
            json=_call_payload("community.get_post", {"post_id": "post-42"}),
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020

        wrong_user = await client.post(
            "/mcp",
            headers=_headers("tools/call", name="community.get_post", user_id="user-2"),
            json=_call_payload("community.get_post", {"post_id": "post-42"}),
        )
        assert wrong_user.status_code == 403
        assert wrong_user.json()["error"]["code"] == -32003
        assert server.calls == []


@pytest.mark.asyncio
async def test_write_requires_trusted_durable_runtime_boundary() -> None:
    server = FakeMCPServer()
    app = create_app(
        server=server,
        conversation_service=FakeConversationService(),
        auth_resolver=FakeAuthResolver(),
        internal_token="runtime-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcp.test",
    ) as client:
        response = await client.post(
            "/mcp",
            headers=_headers("tools/call", name="publication.schedule"),
            json=_call_payload("publication.schedule", {"draft_id": "draft-1", "run_at": "2030-01-01T00:00:00Z"}),
        )
    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["code"] == "PERMISSION_DENIED"
    assert response.json()["result"]["structuredContent"]["request_sent"] is False
    assert server.calls == []


@pytest.mark.asyncio
async def test_trusted_runtime_write_reaches_the_existing_handler_boundary() -> None:
    server = FakeMCPServer()
    app = create_app(
        server=server,
        conversation_service=FakeConversationService(),
        auth_resolver=FakeAuthResolver(),
        internal_token="runtime-secret",
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://mcp.test",
    ) as client:
        response = await client.post(
            "/mcp",
            headers=_headers(
                "tools/call",
                name="publication.schedule",
                runtime_token="runtime-secret",
                agent_run_id="durable-run-1",
            )
            | {"X-GreenBook-Approval-Granted": "true"},
            json=_call_payload(
                "publication.schedule",
                {"draft_id": "draft-1", "run_at": "2030-01-01T00:00:00Z"},
            ),
        )

    assert response.status_code == 200
    assert response.json()["result"]["structuredContent"]["ok"] is True
    assert server.calls[0]["agent_run_id"] == "durable-run-1"
    assert server.calls[0]["approval_granted"] is True


@pytest.mark.asyncio
async def test_schema_validation_and_unknown_tool_are_protocol_safe() -> None:
    server = GreenBookMCPServer(java=object())
    adapter = GreenBookMCPProtocolAdapter(server)
    context = TrustedMCPContext(auth=AUTH, session=SESSION)

    invalid = await adapter.call_tool("community.get_post", {}, context=context)
    assert invalid["ok"] is False
    assert invalid["code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert invalid["request_sent"] is False

    with pytest.raises(MCPProtocolError) as error:
        await adapter.call_tool("interaction.list_comments", {}, context=context)
    assert error.value.code == -32602


@pytest.mark.asyncio
async def test_client_remote_read_is_equivalent_to_local_result() -> None:
    server = FakeMCPServer()
    local_server = FakeMCPServer()
    app = create_app(
        server=server,
        conversation_service=FakeConversationService(),
        auth_resolver=FakeAuthResolver(),
        internal_token="runtime-secret",
    )
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.test/mcp",
        remote_tools={"community.get_post"},
        transport=httpx.ASGITransport(app=app),
    )
    local_client = GreenBookMCPClient(
        local_server,
        transport_mode="local",
    )

    kwargs = {
        "auth": AUTH,
        "session": SESSION,
        "trace_id": "trace-1",
        "agent_run_id": "run-1",
        "tool_call_id": "call-1",
        "post_id": "post-42",
    }
    local_result = await local_client.execute_tool(
        "community.get_post",
        **kwargs,
    )
    remote_result = await client.execute_tool(
        "community.get_post",
        **kwargs,
    )

    assert local_result == {
        "ok": True,
        "code": "OK",
        "data": {"post_id": "post-42"},
        "request_sent": False,
    }
    assert remote_result == local_result
    assert server.calls[0]["agent_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_client_transport_failures_preserve_write_uncertainty() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider down", request=_request)

    unavailable_client = GreenBookMCPClient(
        FakeMCPServer(),
        base_url="http://mcp.test/mcp",
        remote_tools={"community.get_post"},
        transport=httpx.MockTransport(unavailable),
    )
    read_failure = await unavailable_client.execute_tool(
        "community.get_post",
        auth=AUTH,
        session=SESSION,
        post_id="post-1",
    )
    assert read_failure["code"] == "MCP_UNAVAILABLE"
    assert read_failure["state"]["safe_to_retry"] is True

    def response_lost(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=_request)

    write_client = GreenBookMCPClient(
        FakeMCPServer(),
        base_url="http://mcp.test/mcp",
        remote_tools={"content.update_draft"},
        transport=httpx.MockTransport(response_lost),
    )
    write_failure = await write_client.execute_tool(
        "content.update_draft",
        auth=AUTH,
        session=SESSION,
        agent_run_id="run-1",
        draft_id="draft-1",
        title="new title",
    )
    assert write_failure["code"] == "RESULT_UNKNOWN"
    assert write_failure["request_sent"] is None
    assert write_failure["state"]["safe_to_retry"] is False


@pytest.mark.asyncio
async def test_default_mcp_mode_routes_every_active_tool_without_partial_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The old partial remote-tool environment cannot restore 2+12 routing."""

    monkeypatch.delenv("GREENBOOK_MCP_TRANSPORT", raising=False)
    monkeypatch.setenv(
        "GREENBOOK_BUSINESS_MCP_REMOTE_TOOLS",
        "community.get_post",
    )
    server = FakeMCPServer()
    app = create_app(
        server=server,
        conversation_service=FakeConversationService(),
        auth_resolver=FakeAuthResolver(),
        internal_token="runtime-secret",
    )
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.test/mcp",
        runtime_token="runtime-secret",
        transport=httpx.ASGITransport(app=app),
    )

    names = [tool.name for tool in list_tools()]
    assert client.transport_mode == "mcp"
    assert client.remote_tools == set(names)

    for definition in list_tools():
        result = await client.execute_tool(
            definition.name,
            auth=AUTH,
            session=SESSION,
            agent_run_id="run-all-tools",
            approval_granted=True,
        )
        assert result["ok"] is True, definition.name

    assert {call["tool_name"] for call in server.calls} == set(names)
    assert len(server.calls) == len(names)


@pytest.mark.asyncio
async def test_mcp_mode_fails_closed_when_endpoint_is_missing() -> None:
    server = FakeMCPServer()
    client = GreenBookMCPClient(
        server,
        base_url="",
        transport_mode="mcp",
    )

    read_failure = await client.execute_tool(
        "community.get_post",
        auth=AUTH,
        session=SESSION,
        post_id="post-1",
    )
    write_failure = await client.execute_tool(
        "content.update_draft",
        auth=AUTH,
        session=SESSION,
        draft_id="draft-1",
        title="updated",
    )

    assert read_failure["code"] == "MCP_UNAVAILABLE"
    assert read_failure["request_sent"] is False
    assert read_failure["state"]["safe_to_retry"] is True
    assert write_failure["code"] == "MCP_UNAVAILABLE"
    assert write_failure["request_sent"] is False
    assert write_failure["state"]["safe_to_retry"] is True
    assert server.calls == []


@pytest.mark.asyncio
async def test_partial_test_allowlist_fails_closed_without_local_fallback() -> None:
    server = FakeMCPServer()
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.test/mcp",
        remote_tools={"community.get_post"},
        transport_mode="mcp",
    )

    result = await client.execute_tool(
        "content.get_draft",
        auth=AUTH,
        session=SESSION,
    )

    assert result["code"] == "MCP_TOOL_NOT_EXPOSED"
    assert result["request_sent"] is False
    assert server.calls == []


@pytest.mark.asyncio
async def test_remote_provider_failure_never_retries_write_locally() -> None:
    def unavailable(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("provider down", request=_request)

    server = FakeMCPServer()
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.test/mcp",
        transport_mode="mcp",
        transport=httpx.MockTransport(unavailable),
    )

    result = await client.execute_tool(
        "content.update_draft",
        auth=AUTH,
        session=SESSION,
        draft_id="draft-1",
        title="updated",
    )

    assert result["code"] == "MCP_UNAVAILABLE"
    assert result["request_sent"] is False
    assert server.calls == []


@pytest.mark.asyncio
async def test_connect_timeout_before_send_is_unavailable_not_unknown() -> None:
    def connect_timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("provider connect timeout", request=_request)

    server = FakeMCPServer()
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.test/mcp",
        transport_mode="mcp",
        transport=httpx.MockTransport(connect_timeout),
    )

    result = await client.execute_tool(
        "content.update_draft",
        auth=AUTH,
        session=SESSION,
        draft_id="draft-1",
        title="updated",
    )

    assert result["code"] == "MCP_UNAVAILABLE"
    assert result["request_sent"] is False
    assert result["state"]["safe_to_retry"] is True
    assert server.calls == []


@pytest.mark.asyncio
async def test_response_lost_write_never_retries_locally() -> None:
    def response_lost(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=_request)

    server = FakeMCPServer()
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.test/mcp",
        transport_mode="mcp",
        transport=httpx.MockTransport(response_lost),
    )

    result = await client.execute_tool(
        "content.update_draft",
        auth=AUTH,
        session=SESSION,
        draft_id="draft-1",
        title="updated",
    )

    assert result["code"] == "RESULT_UNKNOWN"
    assert result["request_sent"] is None
    assert result["state"]["safe_to_retry"] is False
    assert server.calls == []


@pytest.mark.asyncio
async def test_local_transport_requires_explicit_selection() -> None:
    server = FakeMCPServer()
    client = GreenBookMCPClient(
        server,
        base_url="http://mcp.invalid/mcp",
        transport_mode="local",
    )

    result = await client.execute_tool(
        "community.get_post",
        auth=AUTH,
        session=SESSION,
        post_id="post-local",
    )

    assert result["ok"] is True
    assert client.uses_local_transport is True
    assert [call["tool_name"] for call in server.calls] == ["community.get_post"]
