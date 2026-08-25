"""Contract tests: Error classification by request phase."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from greenbook_java_client.client import JavaClient
from greenbook_contracts import RecoveryAction, SideEffectState, normalize_external_failure


@pytest.fixture
def client():
    return JavaClient(base_url="http://127.0.0.1:9999")


@pytest.mark.asyncio
async def test_connect_error_is_dependency_unavailable(client):
    """ConnectError → JAVA_BACKEND_UNAVAILABLE, request_sent=false, retryable=true."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectError("refused")):
        result = await client.search_posts(query="test")
        assert result.ok is False
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"
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
        assert result.request_sent is None
        assert result.state["side_effect_started"] is True
        assert result.state["side_effect_state"] == "POSSIBLE"
        assert result.state["result_known"] is False


@pytest.mark.asyncio
async def test_read_timeout_has_no_side_effect_and_is_retryable(client):
    with patch.object(client.http, "request", side_effect=httpx.ReadTimeout("read timeout")):
        result = await client.search_posts(query="test")
    assert result.code == "TIMEOUT"
    assert result.retryable is True
    assert result.request_sent is True
    assert result.state["side_effect_started"] is False
    assert result.state["side_effect_state"] == "NONE"


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
async def test_write_connection_reset_is_unknown_outcome(client):
    from greenbook_java_client.models import AgentDraftCreateRequest

    request = AgentDraftCreateRequest(title="Test", content="Body")
    with patch.object(client.http, "request", side_effect=httpx.RemoteProtocolError("reset")):
        result = await client.create_draft(request, bearer_token="t", idempotency_key="stable-key")
    assert result.code == "RESULT_UNKNOWN"
    assert result.request_sent is None
    assert result.state["idempotency_key"] == "stable-key"
    assert result.state["side_effect_started"] is True
    assert result.state["side_effect_state"] == "POSSIBLE"


@pytest.mark.asyncio
async def test_connect_timeout_is_dependency_unavailable(client):
    """ConnectTimeout → JAVA_BACKEND_UNAVAILABLE, request_sent=false."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectTimeout("connect timeout")):
        result = await client.get_post("123")
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"
        assert result.request_sent is False


@pytest.mark.asyncio
async def test_pool_timeout_is_dependency_unavailable(client):
    """PoolTimeout → JAVA_BACKEND_UNAVAILABLE."""
    with patch.object(client.http, "request", side_effect=httpx.PoolTimeout("pool exhausted")):
        result = await client.search_posts(query="test")
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"
        assert result.retryable is True


@pytest.mark.asyncio
async def test_structured_internal_500_is_server_failure(client):
    response = httpx.Response(
        500,
        json={"error": {"code": "INTERNAL_ERROR", "message": "db failure"}},
    )
    with patch.object(client.http, "request", return_value=response):
        result = await client.search_posts(query="test")
    assert result.code == "SERVER_FAILURE"
    assert result.retryable is False
    assert result.request_sent is True


@pytest.mark.asyncio
async def test_structured_field_too_long_is_permanent_input(client):
    response = httpx.Response(
        400,
        json={
            "error": {
                "code": "FIELD_TOO_LONG",
                "message": "field=summary; maxLength=50; actualLength=51",
                "userMessage": "The draft metadata does not meet the publishing requirements.",
                "field": "summary",
                "maxLength": 50,
                "actualLength": 51,
            }
        },
    )
    with patch.object(client.http, "request", return_value=response):
        result = await client.search_posts(query="test")
    assert result.code == "FIELD_TOO_LONG"
    assert result.retryable is False
    assert result.state["max_length"] == 50
    assert result.state["actual_length"] == 51


@pytest.mark.asyncio
async def test_java_business_4xx_is_known_rejection_not_reconciliation(client):
    response = httpx.Response(
        409,
        json={"error": {"code": "BUSINESS_REJECTED", "userMessage": "Draft is locked"}},
    )
    with patch.object(client.http, "request", return_value=response):
        result = await client.search_posts(query="test")

    assert result.code == "BUSINESS_REJECTED"
    assert result.request_sent is True
    assert result.state["side_effect_state"] == "NOT_STARTED"
    failure = normalize_external_failure(result)
    assert failure.side_effect_state is SideEffectState.NOT_STARTED
    assert failure.recovery_action is RecoveryAction.FAIL


@pytest.mark.asyncio
async def test_success_responses_have_request_sent_true(client):
    """2xx responses have request_sent=true."""
    mock_resp = httpx.Response(200, json={"postId": "1", "title": "Test", "description": "", "body": ""})
    with patch.object(client.http, "request", return_value=mock_resp):
        result = await client.get_post("1")
        assert result.ok is True
        assert result.request_sent is True


@pytest.mark.asyncio
async def test_http_503_without_body_is_dependency_unavailable(client):
    """50x responses without structured error → JAVA_BACKEND_UNAVAILABLE."""
    mock_resp = httpx.Response(503, text="Service Unavailable")
    with patch.object(client.http, "request", return_value=mock_resp):
        result = await client.search_posts(query="test")
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"


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
    """NetworkError before request → JAVA_BACKEND_UNAVAILABLE."""
    with patch.object(client.http, "request", side_effect=httpx.NetworkError("network gone")):
        result = await client.search_posts(query="test")
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"
        assert result.request_sent is False


@pytest.mark.asyncio
async def test_no_content_delete_is_a_success(client):
    """204 DELETE responses must not be parsed as JSON."""
    mock_resp = httpx.Response(204)
    with patch.object(client.http, "request", return_value=mock_resp):
        result = await client.cancel_schedule(
            "schedule-1", bearer_token="t", idempotency_key="cancel-1"
        )
    assert result.ok is True
    assert result.code == "OK"
