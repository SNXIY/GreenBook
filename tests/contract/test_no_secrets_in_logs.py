"""Contract tests: No secrets in logs or error outputs."""
from __future__ import annotations

from greenbook_agent_core.middleware import sanitize_headers
from greenbook_contracts.tool_result import ToolResult


class TestNoSecretsInHeaders:
    def test_sanitize_authorization(self) -> None:
        headers = {"Authorization": "Bearer my-secret-token", "Content-Type": "application/json"}
        clean = sanitize_headers(headers)
        assert clean["Authorization"] == "***"
        assert clean["Content-Type"] == "application/json"

    def test_sanitize_cookie(self) -> None:
        headers = {"Cookie": "session=abc123"}
        clean = sanitize_headers(headers)
        assert clean["Cookie"] == "***"

    def test_sanitize_api_key(self) -> None:
        headers = {"X-API-Key": "sk-12345"}
        clean = sanitize_headers(headers)
        assert clean["X-API-Key"] == "***"

    def test_sanitize_auth_token(self) -> None:
        headers = {"X-Auth-Token": "abc"}
        clean = sanitize_headers(headers)
        assert clean["X-Auth-Token"] == "***"


class TestToolResultNoSecrets:
    def test_success_no_token_field(self) -> None:
        r = ToolResult.success({"data": "x"})
        d = r.model_dump()
        assert "token" not in d
        assert "password" not in d
        assert "apikey" not in d
        assert "secret" not in d
        assert "jwt" not in d
        assert "authorization" not in d

    def test_error_no_token_leak(self) -> None:
        r = ToolResult.internal_error("Something failed")
        d = r.model_dump()
        assert "Bearer" not in str(d)
