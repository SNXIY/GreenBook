"""Contract tests: JavaClient error mapping.

Verify that all HTTP error codes are properly classified
and that connection failures return DEPENDENCY_UNAVAILABLE.
"""
from __future__ import annotations

import httpx
import pytest
from greenbook_java_client.client import JavaClient


@pytest.fixture
async def client() -> JavaClient:
    return JavaClient("http://127.0.0.1:1")  # Non-existent port


class TestJavaClientErrorMapping:
    async def test_connection_refused_is_dependency_unavailable(self, client: JavaClient) -> None:
        result = await client.get("/api/v1/posts/search")
        assert result.ok is False
        assert result.code == "DEPENDENCY_UNAVAILABLE"
        assert result.retryable is True
        assert result.request_sent is False

    async def test_result_never_blindly_says_unknown(self, client: JavaClient) -> None:
        """All connection failures must be DEPENDENCY_UNAVAILABLE, not RESULT_UNKNOWN."""
        result = await client.get("/api/v1/health")
        assert result.code != "RESULT_UNKNOWN"
        assert result.code == "DEPENDENCY_UNAVAILABLE"

    async def test_error_messages_are_user_safe(self, client: JavaClient) -> None:
        """Error messages must not expose raw HTTP errors to users."""
        result = await client.get("/api/v1/test")
        assert "All connection attempts failed" not in result.user_message
        assert "提交结果未知" not in result.user_message
        assert "禁止盲目重试" not in result.user_message


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
