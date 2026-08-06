"""Contract tests: JavaClient error mapping uses type-specific API methods."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
from greenbook_java_client.client import JavaClient


@pytest.fixture
def client():
    return JavaClient(base_url="http://127.0.0.1:9999")


@pytest.mark.asyncio
async def test_connection_refused_is_java_backend_unavailable(client):
    """ConnectError → JAVA_BACKEND_UNAVAILABLE, not RESULT_UNKNOWN."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectError("refused")):
        result = await client.search_posts(query="test")
        assert result.ok is False
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"
        assert result.retryable is True
        assert result.request_sent is False


@pytest.mark.asyncio
async def test_result_never_blindly_says_unknown(client):
    """All connection failures must be JAVA_BACKEND_UNAVAILABLE, not RESULT_UNKNOWN."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectError("refused")):
        result = await client.get_post("123")
        assert result.code != "RESULT_UNKNOWN"
        assert result.code == "JAVA_BACKEND_UNAVAILABLE"


@pytest.mark.asyncio
async def test_error_messages_are_user_safe(client):
    """Error messages must not expose raw HTTP errors to users."""
    with patch.object(client.http, "request", side_effect=httpx.ConnectError("Connection refused by target machine")):
        result = await client.search_posts(query="test")
        assert "Connection refused" not in result.user_message
        assert result.user_message != ""


class TestToolResultUserMessages:
    def test_dependency_unavailable_is_user_safe(self) -> None:
        from greenbook_contracts.tool_result import ToolResult
        r = ToolResult.dependency_unavailable("Connection refused")
        assert "unavailable" in r.user_message.lower()
        assert "Connection refused" not in r.user_message

    def test_validation_error_has_user_message(self) -> None:
        from greenbook_contracts.tool_result import ToolResult
        r = ToolResult.validation_error(message="Bad field xyz")
        assert r.user_message != ""

    def test_internal_error_has_user_message(self) -> None:
        from greenbook_contracts.tool_result import ToolResult
        r = ToolResult.internal_error("Stack trace here")
        assert "internal" in r.user_message.lower()
        assert "Stack trace" not in r.user_message
