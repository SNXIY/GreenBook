"""HTTP and SSE contract tests for the durable public Activity feed.

The Activity store is an explicit in-memory test double here; it is not a
claim that Java/LLM services were exercised.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from greenbook_agent_core.activity import UserActivityStore
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.user_activity import (
    UserActivityEvent,
    UserActivityStatus,
    UserActivityType,
)

from apps.agent_api.greenbook_agent_api.api.routes import stream_user_activities
from apps.agent_api.greenbook_agent_api.main import create_app


def _auth(token: str) -> AuthContext:
    if token == "other-token":
        return AuthContext(user_id="user-2", tenant_id="tenant-1", raw_access_token=token)
    return AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token=token)


def _app():
    app = create_app(auth_validator=_auth)
    app.state.conversation_store = {
        "conversation-1": {
            "conversation_id": "conversation-1",
            "user_id": "user-1",
            "tenant_id": "tenant-1",
            "title": "Activity test",
            "created_at": "2026-08-15T00:00:00Z",
            "updated_at": "2026-08-15T00:00:00Z",
            "timezone": "Asia/Shanghai",
            "active_draft_id": None,
            "active_schedule_id": None,
            "active_post_id": None,
            "active_artifact_id": None,
            "active_task_id": None,
            "recent_entities": [],
            "recent_tool_calls": [],
            "pending_approval": None,
            "last_successful_run_id": None,
        }
    }
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}
    app.state.user_activity_store = UserActivityStore()
    return app


def _append(app, activity_type: UserActivityType, status: UserActivityStatus, key: str):
    return app.state.user_activity_store.append(
        UserActivityEvent(
            conversation_id="conversation-1",
            run_id="run-1",
            task_id="task-java",
            activity_type=activity_type,
            status=status,
            display_key="activity.test",
            safe_payload={
                "title": "Java interview",
                "tool_name": "must-not-leak",
                "raw_exception": "must-not-leak",
            },
            terminal=status == UserActivityStatus.COMPLETED,
        ),
        user_id="user-1",
        tenant_id="tenant-1",
        dedupe_key=key,
    )


def test_activity_list_replays_in_order_and_never_returns_private_store_fields() -> None:
    app = _app()
    first = _append(app, UserActivityType.SEARCH_STARTED, UserActivityStatus.IN_PROGRESS, "search")
    second = _append(app, UserActivityType.SEARCH_COMPLETED, UserActivityStatus.COMPLETED, "done")
    client = TestClient(app)

    response = client.get(
        "/api/v1/agent/conversations/conversation-1/activities",
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert [item["sequence"] for item in payload["items"]] == [first.sequence, second.sequence]
    first_item = payload["items"][0]
    assert first_item["activity_id"]
    assert first_item["task_id"] == "task-java"
    # Store ownership and private delivery keys never cross the API boundary.
    assert "user_id" not in first_item
    assert "tenant_id" not in first_item
    assert "dedupe_key" not in first_item
    # The projector itself whitelists payloads. The test verifies that the API
    # does not add Runtime/transport fields on top of that public contract.
    assert "tool_name" not in first_item["safe_payload"]
    assert "raw_exception" not in first_item["safe_payload"]


def test_activity_list_enforces_conversation_ownership_and_cursor() -> None:
    app = _app()
    first = _append(app, UserActivityType.SEARCH_STARTED, UserActivityStatus.IN_PROGRESS, "search")
    second = _append(app, UserActivityType.SEARCH_COMPLETED, UserActivityStatus.COMPLETED, "done")
    client = TestClient(app)

    replay = client.get(
        f"/api/v1/agent/conversations/conversation-1/activities?after_sequence={first.sequence}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert replay.status_code == 200
    assert [item["sequence"] for item in replay.json()["items"]] == [second.sequence]

    denied = client.get(
        "/api/v1/agent/conversations/conversation-1/activities",
        headers={"Authorization": "Bearer other-token"},
    )
    assert denied.status_code == 404


def test_historical_waiting_run_and_approval_activity_are_currently_isolated() -> None:
    app = _app()

    stale_run = SimpleNamespace(
        run_id="stale-run",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        status="RUNNING",
        claimed_by="",
        lease_until="",
        version=1,
        payload={"execution_id": "stale-execution"},
        error_code="",
        error_message="",
        created_at="2026-08-20T00:00:00Z",
        updated_at="2026-08-20T00:00:00Z",
    )

    class DurableRuns:
        def get(self, run_id: str):
            return stale_run if run_id == stale_run.run_id else None

        def list_recent(self, *, limit: int):
            return [stale_run]

    class Executions:
        def find_by_id(self, execution_id: str):
            if execution_id == "stale-execution":
                return SimpleNamespace(status="WAITING_APPROVAL")
            return None

    app.state.agent_run_store = DurableRuns()
    app.state.execution_repository = Executions()
    event = app.state.user_activity_store.append(
        UserActivityEvent(
            conversation_id="conversation-1",
            run_id="stale-run",
            task_id="stale-task",
            activity_type=UserActivityType.NEEDS_APPROVAL,
            status=UserActivityStatus.WAITING_APPROVAL,
            display_key="activity.approval.required",
            safe_payload={"approval_id": "stale-approval", "title": "历史草稿"},
        ),
        user_id="user-1",
        tenant_id="tenant-1",
        dedupe_key="stale-approval",
    )
    client = TestClient(app)

    activity = client.get(
        "/api/v1/agent/conversations/conversation-1/activities",
        headers={"Authorization": "Bearer test-token"},
    )
    assert activity.status_code == 200
    assert activity.json() == {"items": [], "next_cursor": event.sequence}

    run = client.get(
        "/api/v1/agent/runs/stale-run",
        headers={"Authorization": "Bearer test-token"},
    )
    assert run.status_code == 404

    runs = client.get(
        "/api/v1/agent/runs",
        headers={"Authorization": "Bearer test-token"},
    )
    assert runs.status_code == 200
    assert all(item["run_id"] != "stale-run" for item in runs.json())


class _DirectStreamRequest:
    def __init__(self, app) -> None:
        self.app = app
        self.state = SimpleNamespace(auth_context=_auth("test-token"))
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > 1


@pytest.mark.asyncio
async def test_activity_sse_uses_sequence_id_and_last_event_replay_cursor() -> None:
    app = _app()
    first = _append(app, UserActivityType.SEARCH_STARTED, UserActivityStatus.IN_PROGRESS, "search")
    second = _append(app, UserActivityType.SEARCH_COMPLETED, UserActivityStatus.COMPLETED, "done")
    response = await stream_user_activities(
        "conversation-1",
        _DirectStreamRequest(app),
        after_sequence=0,
        last_event_id=str(first.sequence),
    )
    frame = await anext(response.body_iterator)
    assert frame.startswith(f"id: {second.sequence}\nevent: user_activity\n")
    data = next(line[5:].strip() for line in frame.splitlines() if line.startswith("data:"))
    event = json.loads(data)
    assert event["activity_id"] == second.activity_id
    assert event["sequence"] == second.sequence
