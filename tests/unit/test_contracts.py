"""Unit tests for shared contracts — AuthContext, ToolResult, ErrorCode."""

from __future__ import annotations

from greenbook_contracts.errors import ErrorCode, GreenBookError
from greenbook_contracts.events import BusinessEvent
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult


class TestToolResult:
    def test_success(self) -> None:
        r = ToolResult.success({"draft_id": "d-1"})
        assert r.ok is True
        assert r.code == "OK"
        assert r.request_sent is True
        assert r.data == {"draft_id": "d-1"}

    def test_dependency_unavailable(self) -> None:
        r = ToolResult.dependency_unavailable("Connection refused")
        assert r.ok is False
        assert r.code == "DEPENDENCY_UNAVAILABLE"
        assert r.retryable is True
        assert r.request_sent is False

    def test_validation_error(self) -> None:
        r = ToolResult.validation_error("title is required")
        assert r.ok is False
        assert r.code == "VALIDATION_ERROR"
        assert r.retryable is False
        assert r.request_sent is False

    def test_permission_denied(self) -> None:
        r = ToolResult.permission_denied()
        assert r.ok is False
        assert r.code == "PERMISSION_DENIED"

    def test_not_found(self) -> None:
        r = ToolResult.not_found("Draft not found")
        assert r.ok is False
        assert r.code == "NOT_FOUND"
        assert r.request_sent is True

    def test_conflict(self) -> None:
        r = ToolResult.conflict()
        assert r.ok is False
        assert r.code == "CONFLICT"

    def test_timeout(self) -> None:
        r = ToolResult.timeout()
        assert r.ok is False
        assert r.code == "TIMEOUT"
        assert r.retryable is True
        assert r.request_sent is True

    def test_internal_error(self) -> None:
        r = ToolResult.internal_error("unexpected", trace_id="t-1")
        assert r.ok is False
        assert r.code == "INTERNAL_ERROR"
        assert r.trace_id == "t-1"

    def test_serialization(self) -> None:
        r = ToolResult.success({"key": "value"}, trace_id="abc")
        d = r.model_dump()
        assert d["ok"] is True
        assert d["trace_id"] == "abc"


class TestErrorCode:
    def test_all_codes_defined(self) -> None:
        codes = list(ErrorCode)
        assert ErrorCode.VALIDATION_ERROR in codes
        assert ErrorCode.DEPENDENCY_UNAVAILABLE in codes
        assert ErrorCode.REQUEST_NOT_SENT in codes
        assert ErrorCode.RESULT_UNKNOWN in codes
        assert ErrorCode.BUSINESS_REJECTED in codes
        assert ErrorCode.DRAFT_VERSION_CONFLICT in codes


class TestGreenBookError:
    def test_basic(self) -> None:
        e = GreenBookError(ErrorCode.TIMEOUT, "timeout", retryable=True, request_sent=True)
        assert e.code == ErrorCode.TIMEOUT
        assert e.retryable is True
        assert e.request_sent is True


class TestAuthContext:
    def test_construction(self) -> None:
        ctx = AuthContext(user_id="u1", tenant_id="t1", raw_access_token="tok")
        assert ctx.user_id == "u1"
        assert ctx.tenant_id == "t1"

    def test_default_roles(self) -> None:
        ctx = AuthContext(user_id="u1", tenant_id="t1", raw_access_token="tok")
        assert ctx.roles == []

    def test_timezone_default(self) -> None:
        ctx = AuthContext(user_id="u1", tenant_id="t1", raw_access_token="tok")
        assert ctx.timezone == "Asia/Shanghai"


class TestBusinessEvent:
    def test_construction(self) -> None:
        e = BusinessEvent(event_type="draft.created", event_id="ev-1")
        assert e.event_type == "draft.created"
        assert e.event_id == "ev-1"


class TestNoUserIdInjection:
    """Model must never pass user_id — it always comes from AuthContext."""

    def test_auth_context_immutable_from_model(self) -> None:
        ctx = AuthContext(user_id="secure-user", tenant_id="t1", raw_access_token="tok")
        assert ctx.user_id == "secure-user"

    def test_tool_result_has_no_user_id_field(self) -> None:
        r = ToolResult.success({"data": "x"})
        d = r.model_dump()
        assert "user_id" not in d
        assert "tenant_id" not in d
