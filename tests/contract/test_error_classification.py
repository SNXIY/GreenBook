"""Contract tests: Error classification by request phase."""

from __future__ import annotations

import pytest
import httpx
from unittest.mock import AsyncMock, patch

from greenbook_java_client.client import JavaClient
from greenbook_contracts.tool_result import ToolResult


@pytest.fixture
def client():
    return JavaClient(base_url="http://127.0.0.1:9999")


@pytest.mark.asyncio
async def test_connect_error_is_dependency_unavailable(client):
    """ConnectError → DEPENDENCY_UNAVAILABLE, request_sent=false, retryable=true."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectError("refused")):
        result = await client.search_posts(query="test")
        assert result.ok is False
        assert result.code == "DEPENDENCY_UNAVAILABLE"
        assert result.retryable is True
        assert result.request_sent is False


@pytest.mark.asyncio
async def test_read_timeout_read_is_retryable(client):
    """ReadTimeout on GET → TIMEOUT, retryable=true."""
    with patch.object(client.http, "request", side_effect=httpx.ReadTimeout("read timeout")):
        result = await client.search_posts(query="test")
        assert result.code == "TIMEOUT"
        assert result.retryable is True


@pytest.mark.asyncio
async def test_read_timeout_write_is_result_unknown(client):
    """ReadTimeout on write → RESULT_UNKNOWN, retryable=false."""
    from greenbook_java_client.models import AgentDraftCreateRequest
    req = AgentDraftCreateRequest(title="Test", content="Body")
    with patch.object(client.http, "request", side_effect=httpx.ReadTimeout("read timeout")):
        result = await client.create_draft(req, bearer_token="t", idempotency_key="k1")
        assert result.code == "RESULT_UNKNOWN"
        assert result.retryable is False
        assert result.request_sent is True


@pytest.mark.asyncio
async def test_write_timeout_is_not_sent(client):
    """WriteTimeout → REQUEST_NOT_SENT, retryable=true."""
    from greenbook_java_client.models import AgentDraftCreateRequest
    req = AgentDraftCreateRequest(title="Test", content="Body")
    with patch.object(client.http, "request", side_effect=httpx.WriteTimeout("write timeout")):
        result = await client.create_draft(req, bearer_token="t", idempotency_key="k1")
        assert result.code == "REQUEST_NOT_SENT"
        assert result.retryable is True
        assert result.request_sent is False


@pytest.mark.asyncio
async def test_connect_timeout_is_dependency_unavailable(client):
    """ConnectTimeout → DEPENDENCY_UNAVAILABLE, request_sent=false."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectTimeout("connect timeout")):
        result = await client.get_post("123")
        assert result.code == "DEPENDENCY_UNAVAILABLE"
        assert result.request_sent is False


@pytest.mark.asyncio
async def test_pool_timeout_is_dependency_unavailable(client):
    """PoolTimeout → DEPENDENCY_UNAVAILABLE."""
    with patch.object(client.http, "request", side_effect=httpx.PoolTimeout("pool exhausted")):
        result = await client.search_posts(query="test")
        assert result.code == "DEPENDENCY_UNAVAILABLE"
        assert result.retryable is True


@pytest.mark.asyncio
async def test_success_responses_have_request_sent_true(client):
    """2xx responses have request_sent=true."""
    mock_resp = httpx.Response(200, json={"postId": "1", "title": "Test", "description": "", "body": ""})
    with patch.object(client.http, "request", return_value=mock_resp):
        result = await client.get_post("1")
        assert result.ok is True
        assert result.request_sent is True


@pytest.mark.asyncio
async def test_http_500_is_dependency_unavailable(client):
    """50x responses without structured error → DEPENDENCY_UNAVAILABLE."""
    mock_resp = httpx.Response(503, text="Service Unavailable")
    with patch.object(client.http, "request", return_value=mock_resp):
        result = await client.search_posts(query="test")
        assert result.code == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_draft_version_conflict_code_mapped(client):
    """DRAFT_VERSION_CONFLICT error code from Java is correctly mapped."""
    error_json = {"error": {"code": "DRAFT_VERSION_CONFLICT", "message": "stale"}}
    mock_resp = httpx.Response(409, json=error_json)
    with patch.object(client.http, "request", return_value=mock_resp):
        from greenbook_java_client.models import AgentDraftUpdateRequest
        req = AgentDraftUpdateRequest(title="x", content="y")
        result = await client.update_draft("1", req, bearer_token="t", idempotency_key="k1")
        assert result.code == "DRAFT_VERSION_CONFLICT"


@pytest.mark.asyncio
async def test_network_error_is_dependency_unavailable(client):
    """NetworkError before request → DEPENDENCY_UNAVAILABLE."""
    with patch.object(client.http, "request", side_effect=httpx.NetworkError("network gone")):
        result = await client.search_posts(query="test")
        assert result.code == "DEPENDENCY_UNAVAILABLE"
        assert result.request_sent is False
