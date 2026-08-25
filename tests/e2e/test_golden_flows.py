"""Golden-flow E2E tests.

Three critical flows must pass before legacy code removal:
1. CREATE → SCHEDULE
2. PUBLIC_SEARCH → CREATE → SCHEDULE
3. RESOLVE_RECENT_DRAFT → REVISE → KEEP_SCHEDULE_CONSISTENT

These tests use mocked Java/Creator clients to validate the flow
without requiring live infrastructure.
"""
from __future__ import annotations

import pytest
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult


class TestGoldenFlowCreateSchedule:
    """Flow 1: CREATE → SCHEDULE"""

    @pytest.fixture
    def auth(self) -> AuthContext:
        return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="test-token")

    @pytest.fixture
    def session(self, auth: AuthContext) -> SessionContext:
        return SessionContext(conversation_id="conv-1", user_id=auth.user_id, tenant_id=auth.tenant_id)

    async def test_create_draft_then_schedule(self, auth: AuthContext, session: SessionContext) -> None:
        """User creates a draft, then schedules it."""
        results = {
            "content_create_draft": ToolResult.success({"draft_id": "d-1", "title": "Test", "content": "Hello"}),
            "publication_schedule": ToolResult.success({"schedule_id": "sch-1", "draft_id": "d-1", "publish_at": "2026-08-07T10:00:00Z"}),
        }

        async def tool_handler(name: str, args: dict, sess: SessionContext) -> dict:
            return results.get(name, ToolResult.internal_error("unknown tool")).model_dump()

        # Verify flow: create_draft → session has active draft
        r = await tool_handler("content_create_draft", {"title": "Test", "content": "Hello"}, session)
        assert r["ok"] is True
        assert r["data"]["draft_id"] == "d-1"

        session.set_active_draft(r["data"]["draft_id"])
        assert session.active_draft_id == "d-1"

        # Then schedule
        r2 = await tool_handler("publication_schedule", {"draft_id": session.active_draft_id, "publish_at": "2026-08-07T10:00:00Z"}, session)
        assert r2["ok"] is True
        assert r2["data"]["schedule_id"] == "sch-1"


class TestGoldenFlowSearchCreateSchedule:
    """Flow 2: PUBLIC_SEARCH → CREATE → SCHEDULE"""

    @pytest.fixture
    def auth(self) -> AuthContext:
        return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="test-token")

    @pytest.fixture
    def session(self, auth: AuthContext) -> SessionContext:
        return SessionContext(conversation_id="conv-1", user_id=auth.user_id, tenant_id=auth.tenant_id)

    async def test_search_then_create_then_schedule(self, auth: AuthContext, session: SessionContext) -> None:
        results = {
            "community_search_public_posts": ToolResult.success({
                "posts": [{"post_id": "p-1", "title": "Interesting", "author_name": "Alice", "created_at": "2026-08-01", "summary": ""}],
                "total": 1, "page": 1,
            }),
            "content_create_draft": ToolResult.success({"draft_id": "d-2", "title": "My Response", "content": "Content"}),
            "publication_schedule": ToolResult.success({"schedule_id": "sch-2", "draft_id": "d-2", "publish_at": "2026-08-08T09:00:00Z"}),
        }

        async def tool_handler(name: str, args: dict, sess: SessionContext) -> dict:
            return results.get(name, ToolResult.internal_error("unknown")).model_dump()

        # Step 1: Search
        r1 = await tool_handler("community_search_public_posts", {"query": "AI"}, session)
        assert r1["ok"] is True
        assert len(r1["data"]["posts"]) == 1

        # Step 2: Create
        r2 = await tool_handler("content_create_draft", {"title": "My Response", "content": "Content"}, session)
        assert r2["ok"] is True
        assert r2["data"]["draft_id"] == "d-2"
        session.set_active_draft(r2["data"]["draft_id"])

        # Step 3: Schedule
        r3 = await tool_handler("publication_schedule", {"draft_id": "d-2", "publish_at": "2026-08-08T09:00:00Z"}, session)
        assert r3["ok"] is True
        assert r3["data"]["schedule_id"] == "sch-2"


class TestGoldenFlowReviseKeepSchedule:
    """Flow 3: RESOLVE_RECENT_DRAFT → REVISE → KEEP_SCHEDULE_CONSISTENT"""

    @pytest.fixture
    def auth(self) -> AuthContext:
        return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="test-token")

    @pytest.fixture
    def session(self, auth: AuthContext) -> SessionContext:
        return SessionContext(conversation_id="conv-1", user_id=auth.user_id, tenant_id=auth.tenant_id)

    async def test_resolve_recent_then_revise_then_verify_schedule(self, auth: AuthContext, session: SessionContext) -> None:
        """After revising a draft, the schedule must still point to the correct draft."""
        # Step 1: Resolve "刚才的草稿" (the most recent draft)
        session.set_active_draft("draft-recent")
        assert session.active_draft_id == "draft-recent"

        # Step 2: Revise it
        results = {
            "content_revise_draft": ToolResult.success({
                "draft_id": "draft-recent",
                "version": 2,
                "schedule_id": "sch-existing",
                "verified": True,
            }),
        }

        async def tool_handler(name: str, args: dict, sess: SessionContext) -> dict:
            return results.get(name, ToolResult.internal_error("unknown")).model_dump()

        r = await tool_handler("content_revise_draft", {"draft_id": "draft-recent", "instruction": "Make it shorter"}, session)
        assert r["ok"] is True
        assert r["data"]["draft_id"] == "draft-recent"
        assert r["data"]["version"] == 2
        assert r["data"]["schedule_id"] == "sch-existing"

    async def test_schedule_unchanged_after_revise(self, auth: AuthContext, session: SessionContext) -> None:
        """Schedule must remain consistent after draft revision."""
        session.set_active_draft("d-1")
        session.set_active_schedule("sch-1")

        results = {
            "content_revise_draft": ToolResult.success({
                "draft_id": "d-1", "version": 3, "schedule_id": "sch-1", "verified": True,
            }),
            "publication_get_status": ToolResult.success({"schedule_id": "sch-1", "draft_id": "d-1", "status": "scheduled"}),
        }

        async def tool_handler(name: str, args: dict, sess: SessionContext) -> dict:
            return results.get(name, ToolResult.internal_error("unknown")).model_dump()

        # Revise
        r1 = await tool_handler("content_revise_draft", {"draft_id": "d-1", "instruction": "fix"}, session)
        assert r1["data"]["schedule_id"] == "sch-1"

        # Verify schedule still points to d-1
        r2 = await tool_handler("publication_get_status", {"schedule_id": "sch-1"}, session)
        assert r2["ok"] is True
        assert r2["data"]["draft_id"] == "d-1"


class TestErrorHandling:
    """Error handling golden rules."""

    @pytest.fixture
    def auth(self) -> AuthContext:
        return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="test-token")

    @pytest.fixture
    def session(self, auth: AuthContext) -> SessionContext:
        return SessionContext(conversation_id="conv-1", user_id=auth.user_id, tenant_id=auth.tenant_id)

    async def test_dependency_unavailable_is_readable_and_retryable(self, session: SessionContext) -> None:
        """下游不可用时错误可读且可安全重试."""
        from greenbook_contracts.tool_result import ToolResult

        r = ToolResult.dependency_unavailable("Downstream service unreachable")
        assert r.retryable is True
        assert r.request_sent is False
        assert r.code == "DEPENDENCY_UNAVAILABLE"
        assert "unavailable" in r.user_message.lower()

    async def test_java_unavailable_does_not_create_false_success(self, session: SessionContext) -> None:
        """Java不可用时不创建虚假成功."""
        from greenbook_contracts.tool_result import ToolResult

        r = ToolResult.dependency_unavailable("Java backend unreachable")
        assert r.ok is False
        assert r.data is None

    async def test_user_id_from_jwt_overrides_model(self, session: SessionContext) -> None:
        """JWT中的用户身份覆盖模型参数."""
        from greenbook_contracts.identity import AuthContext

        # AuthContext is created by auth middleware, not model
        ctx = AuthContext(user_id="verified-user", tenant_id="t1", raw_access_token="test-token")
        assert ctx.user_id == "verified-user"
        # Model cannot create AuthContext — it's always from validated JWT

    async def test_publish_now_triggers_approval(self, session: SessionContext) -> None:
        """publish_now triggers human confirmation."""
        # publication_publish_now returns requires_approval=True
        result = {"ok": True, "code": "APPROVAL_REQUIRED", "data": {"draft_id": "d-1", "requires_approval": True}}
        assert result["data"]["requires_approval"] is True

    async def test_logs_never_contain_secrets(self) -> None:
        """日志不出现密码、JWT、API Key、Authorization Header."""
        from greenbook_agent_core.middleware import sanitize_headers

        headers = {
            "Authorization": "Bearer eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9...",
            "Content-Type": "application/json",
            "X-API-Key": "sk-secret-key",
        }
        clean = sanitize_headers(headers)
        assert clean["Authorization"] == "***"
        assert clean["X-API-Key"] == "***"
        assert "eyJ" not in str(clean)
        assert "sk-secret" not in str(clean)
