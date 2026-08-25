"""Contract tests: User isolation — conversations, runs, approvals are scoped to user+tenant."""

from __future__ import annotations

import pytest
from greenbook_contracts.identity import AuthContext
from starlette.testclient import TestClient

from apps.agent_api.greenbook_agent_api.main import create_app


@pytest.fixture
def client():
    def test_auth(token: str) -> AuthContext:
        user_id, _, tenant_id = token.partition(":")
        return AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            raw_access_token=token,
        )

    app = create_app(auth_validator=test_auth)
    # Initialize state manually (lifespan doesn't run under sync TestClient)
    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}
    return TestClient(app)


def _make_auth(user_id: str = "user-a", tenant_id: str = "t1") -> AuthContext:
    return AuthContext(
        user_id=user_id, tenant_id=tenant_id,
        raw_access_token="test-token",
    )


def _make_headers(auth: AuthContext | None = None) -> dict[str, str]:
    if auth is None:
        auth = _make_auth()
    return {"Authorization": f"Bearer {auth.user_id}:{auth.tenant_id}"}


def _seed_conversation(store: dict, conv_id: str, user_id: str, tenant_id: str,
                       title: str = "", updated_at: str = "2026-08-01T00:00:00Z"):
    store[conv_id] = {
        "conversation_id": conv_id, "user_id": user_id, "tenant_id": tenant_id,
        "title": title, "created_at": "2026-08-01T00:00:00Z",
        "updated_at": updated_at,
        "active_draft_id": None, "active_schedule_id": None,
        "active_post_id": None, "recent_entities": [], "recent_tool_calls": [],
        "pending_approval": None, "last_successful_run_id": None,
        "timezone": "Asia/Shanghai",
    }


# ── Unauthenticated tests ─────────────────────────────────────────

class TestUnauthenticatedAccess:
    def test_list_conversations_unauth_returns_401(self, client):
        resp = client.get("/api/v1/agent/conversations")
        assert resp.status_code == 401

    def test_create_conversation_unauth_returns_401(self, client):
        resp = client.post("/api/v1/agent/conversations", json={"title": "x"})
        assert resp.status_code == 401

    def test_send_message_unauth_returns_401(self, client):
        resp = client.post("/api/v1/agent/conversations/c1/messages", json={"content": "hi"})
        assert resp.status_code == 401

    def test_get_run_unauth_returns_401(self, client):
        resp = client.get("/api/v1/agent/runs/r1")
        assert resp.status_code == 401

    def test_approve_unauth_returns_401(self, client):
        resp = client.post(
            "/api/v1/agent/executions/e1/approve",
            json={"decision": "APPROVE"},
        )
        assert resp.status_code == 401

    def test_reject_unauth_returns_401(self, client):
        resp = client.post(
            "/api/v1/agent/runs/r1/approvals/ap1",
            json={"decision": "REJECT"},
        )
        assert resp.status_code == 401


# ── Ownership tests ────────────────────────────────────────────────

class TestConversationOwnership:
    def test_post_context_is_persisted_and_filterable(self, client):
        response = client.post(
            "/api/v1/agent/conversations",
            json={"title": "post context", "surface": "POST", "context_post_id": "post-42"},
            headers=_make_headers(),
        )
        assert response.status_code == 200
        conversation_id = response.json()["conversation_id"]
        assert client.app.state.conversation_store[conversation_id]["active_post_id"] == "post-42"

        matching = client.get(
            "/api/v1/agent/conversations?context_post_id=post-42",
            headers=_make_headers(),
        )
        assert matching.status_code == 200
        assert [item["conversation_id"] for item in matching.json()["items"]] == [conversation_id]

        unrelated = client.get(
            "/api/v1/agent/conversations?context_post_id=post-99",
            headers=_make_headers(),
        )
        assert unrelated.status_code == 200
        assert unrelated.json()["items"] == []

    def test_empty_list_for_new_user(self, client):
        resp = client.get("/api/v1/agent/conversations", headers=_make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_user_a_sees_only_own(self, client):
        store = client.app.state.conversation_store
        _seed_conversation(store, "c-a", "user-a", "t1", "A")
        _seed_conversation(store, "c-b", "user-b", "t1", "B")

        resp = client.get("/api/v1/agent/conversations",
                          headers=_make_headers(_make_auth("user-a", "t1")))
        assert resp.status_code == 200
        ids = {c["conversation_id"] for c in resp.json()["items"]}
        assert "c-a" in ids
        assert "c-b" not in ids

    def test_user_b_sees_only_own(self, client):
        store = client.app.state.conversation_store
        _seed_conversation(store, "c-a", "user-a", "t1", "A")
        _seed_conversation(store, "c-b", "user-b", "t1", "B")

        resp = client.get("/api/v1/agent/conversations",
                          headers=_make_headers(_make_auth("user-b", "t1")))
        assert resp.status_code == 200
        ids = {c["conversation_id"] for c in resp.json()["items"]}
        assert "c-b" in ids
        assert "c-a" not in ids

    def test_different_tenant_cannot_see(self, client):
        store = client.app.state.conversation_store
        _seed_conversation(store, "c-t1", "user-a", "t1", "T1")

        resp = client.get("/api/v1/agent/conversations",
                          headers=_make_headers(_make_auth("user-a", "t2")))
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


class TestConversationResponseSanitization:
    def test_no_sensitive_fields_in_response(self, client):
        store = client.app.state.conversation_store
        _seed_conversation(store, "c-1", "user-a", "t1", "Chat")

        resp = client.get("/api/v1/agent/conversations", headers=_make_headers())
        assert resp.status_code == 200
        item = resp.json()["items"][0]
        for forbidden in ("raw_access_token", "token", "password", "api_key",
                          "request_hash", "recent_tool_calls"):
            assert forbidden not in item, f"{forbidden} leaked in response"

    def test_created_conversation_visible_to_owner(self, client):
        resp = client.post("/api/v1/agent/conversations",
                           json={"title": "My Chat"}, headers=_make_headers())
        assert resp.status_code == 200
        conv_id = resp.json()["conversation_id"]

        resp2 = client.get("/api/v1/agent/conversations", headers=_make_headers())
        assert conv_id in [c["conversation_id"] for c in resp2.json()["items"]]

    def test_other_user_does_not_see(self, client):
        resp = client.post("/api/v1/agent/conversations",
                           json={"title": "A"}, headers=_make_headers(_make_auth("u-a")))
        assert resp.status_code == 200

        resp2 = client.get("/api/v1/agent/conversations",
                           headers=_make_headers(_make_auth("u-b")))
        assert resp2.json()["total"] == 0


class TestConversationPagination:
    def test_page_size_and_order(self, client):
        store = client.app.state.conversation_store
        for i in range(1, 26):
            _seed_conversation(store, f"c-{i:02d}", "user-a", "t1",
                               updated_at=f"2026-08-{i:02d}T00:00:00Z")

        resp = client.get("/api/v1/agent/conversations?page=1&size=10",
                          headers=_make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) == 10
        assert data["total"] == 25
        # First item should be newest (c-25)
        assert data["items"][0]["conversation_id"] == "c-25"


class TestRunOwnership:
    def test_owner_can_see_run(self, client):
        client.app.state.run_store["r-1"] = {
            "run_id": "r-1", "conversation_id": "c-1",
            "user_id": "user-a", "tenant_id": "t1",
            "status": "COMPLETED", "content": "ok",
            "trace_id": "t1", "tool_rounds": 0, "events": [],
        }
        resp = client.get("/api/v1/agent/runs/r-1", headers=_make_headers(_make_auth("user-a")))
        assert resp.status_code == 200

    def test_other_user_gets_404(self, client):
        client.app.state.run_store["r-1"] = {
            "run_id": "r-1", "conversation_id": "c-1",
            "user_id": "user-a", "tenant_id": "t1",
            "status": "COMPLETED", "content": "ok",
            "trace_id": "t1", "tool_rounds": 0, "events": [],
        }
        resp = client.get("/api/v1/agent/runs/r-1", headers=_make_headers(_make_auth("user-b")))
        assert resp.status_code == 404

    def test_nonexistent_returns_404(self, client):
        resp = client.get("/api/v1/agent/runs/nope", headers=_make_headers())
        assert resp.status_code == 404

    def test_run_response_carries_created_and_updated_at(self, client):
        client.app.state.run_store["r-1"] = {
            "run_id": "r-1", "conversation_id": "c-1",
            "user_id": "user-a", "tenant_id": "t1",
            "status": "COMPLETED", "content": "ok",
            "trace_id": "t1", "tool_rounds": 0, "events": [],
            "created_at": "2026-08-01T00:00:00Z",
            "updated_at": "2026-08-01T01:00:00Z",
        }
        resp = client.get("/api/v1/agent/runs/r-1", headers=_make_headers(_make_auth("user-a")))
        assert resp.status_code == 200
        body = resp.json()
        assert body["created_at"] == "2026-08-01T00:00:00Z"
        assert body["updated_at"] == "2026-08-01T01:00:00Z"

    def test_list_runs_sorts_by_updated_at_without_crashing(self, client):
        # Regression: the frontend sorts active runs by created_at; the API
        # must return stable time fields for every run so multiple active
        # runs in one conversation cannot break the panel restore path.
        store = client.app.state.run_store
        for i, run_id in enumerate(("r-old", "r-new")):
            store[run_id] = {
                "run_id": run_id, "conversation_id": "c-1",
                "user_id": "user-a", "tenant_id": "t1",
                "status": "RUNNING", "content": f"work {i}",
                "trace_id": f"t-{i}", "tool_rounds": 0, "events": [],
                "created_at": f"2026-08-0{i+1}T00:00:00Z",
                "updated_at": f"2026-08-0{i+1}T00:00:00Z",
            }
        resp = client.get("/api/v1/agent/runs", headers=_make_headers(_make_auth("user-a")))
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 2
        assert items[0]["run_id"] == "r-new"  # newest first
        for item in items:
            assert item["created_at"]
            assert item["updated_at"]


class TestApprovalOwnership:
    """Approval decisions are ownership-checked by the durable
    ``ApprovalRuntimeService``; the route layer maps a foreign user to 404.
    The legacy in-memory ``/approvals/{id}/approve|reject`` writes were
    removed in Phase 4 — see ``test_phase17b_human_control_runtime.py`` for
    the durable service ownership test."""

    def test_legacy_approval_write_endpoints_are_gone(self, client):
        assert client.post(
            "/api/v1/agent/approvals/ap-1/approve",
            json={"decision": "APPROVE"},
            headers=_make_headers(_make_auth("user-a")),
        ).status_code == 404
        assert client.post(
            "/api/v1/agent/approvals/ap-1/reject",
            headers=_make_headers(_make_auth("user-a")),
        ).status_code == 404
