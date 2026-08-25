"""Immediate-accept message entry tests.

These tests exercise the current production contract:

POST -> durable AgentRun(ACCEPTED) -> claim -> AgentRunner -> terminal state.

The HTTP response is intentionally not the RuntimeResult.  No synchronous
adapter fallback is installed by this fixture.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
import sqlalchemy as sa
from greenbook_agent_api.runner import (
    RUN_ACCEPTED,
    RUN_COMPLETED,
    RUN_FAILED,
    AgentRunEventStore,
    AgentRunner,
    AgentRunStore,
)
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from apps.agent_api.greenbook_agent_api.main import create_app
from apps.agent_api.greenbook_agent_api.models.runtime_result import RuntimeResult


class _RecordingRuntimeAdapter:
    def __init__(self, result_factory: Callable[[dict[str, Any]], RuntimeResult]) -> None:
        self.result_factory = result_factory
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> RuntimeResult:
        self.calls.append(kwargs)
        return self.result_factory(kwargs)


@dataclass
class _Harness:
    client: TestClient
    adapter: _RecordingRuntimeAdapter
    run_store: AgentRunStore
    runner: AgentRunner
    engine: Any

    def close(self) -> None:
        self.engine.dispose()


def _make_harness(
    result_factory: Callable[[dict[str, Any]], RuntimeResult],
    validator: Callable[[str], AuthContext] | None = None,
) -> _Harness:
    def validate(token: str) -> AuthContext:
        if validator is not None:
            return validator(token)
        return AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token=token)

    app = create_app(auth_validator=validate)
    engine = sa.create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    run_store = AgentRunStore(engine, create_tables=True)
    event_store = AgentRunEventStore()
    adapter = _RecordingRuntimeAdapter(result_factory)

    # The durable store is the only Run contract under test.  The in-memory
    # app.state.run_store created by create_app remains only the compatibility
    # projection used by existing read endpoints; it is not used for claims.
    app.state.agent_run_store = run_store
    app.state.agent_run_event_store = event_store
    # create_app wires these memory projections during application startup;
    # TestClient is intentionally not used as a lifespan context here because
    # the test drives one claimed Run deterministically.  These are request
    # projections only, never the durable Run store under test.
    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}

    async def execute(run: Any) -> RuntimeResult:
        payload = dict(run.payload or {})
        return await adapter.execute(
            message=str(payload.get("message") or ""),
            run_id=run.run_id,
            session=SessionContext.model_validate(payload.get("session") or {}),
        )

    async def result_handler(_run: Any, _result: RuntimeResult) -> None:
        # AgentRunner owns the durable Run terminal transition.  Projection
        # behavior is covered by the dedicated result-publisher tests.
        return None

    runner = AgentRunner(
        run_store=run_store,
        event_store=event_store,
        execute=execute,
        result_handler=result_handler,
        worker_id="test-agent-runner",
        poll_interval_seconds=0.01,
    )
    return _Harness(TestClient(app), adapter, run_store, runner, engine)


def _create_conversation(client: TestClient, *, active_task_id: str | None = None) -> str:
    response = client.post(
        "/api/v1/agent/conversations",
        json={"title": "runtime migration"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert response.status_code == 200
    conversation_id = response.json()["conversation_id"]
    if active_task_id:
        client.app.state.conversation_store[conversation_id]["active_task_id"] = active_task_id
    return conversation_id


def _drive_one_claimed_run(harness: _Harness, run_id: str) -> None:
    claimed = harness.run_store.claim(
        worker_id="test-agent-runner",
        lease_seconds=300,
        limit=1,
    )
    assert [run.run_id for run in claimed] == [run_id]
    asyncio.run(harness.runner._process(claimed[0]))


@pytest.mark.parametrize("message", ["帮我写一篇AI Agent学习路线帖子"])
def test_create_content_message_persists_then_runner_completes(message: str) -> None:
    def result_factory(kwargs: dict[str, Any]) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            task_id="task-create",
            plan_id="plan-create",
            execution_id="execution-create",
            execution_path="runtime",
        )

    harness = _make_harness(result_factory)
    try:
        conversation_id = _create_conversation(harness.client)
        response = harness.client.post(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            json={"content": message},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["status"] == RUN_ACCEPTED
        assert payload["run_id"]
        assert "execution_id" not in payload or payload["execution_id"] is None

        accepted = harness.run_store.get(payload["run_id"])
        assert accepted is not None
        assert accepted.status == RUN_ACCEPTED
        assert accepted.conversation_id == conversation_id
        assert accepted.payload["message"] == message
        assert harness.adapter.calls == []

        _drive_one_claimed_run(harness, payload["run_id"])

        completed = harness.run_store.get(payload["run_id"])
        assert completed is not None
        assert completed.status == RUN_COMPLETED
        assert harness.adapter.calls[0]["message"] == message
    finally:
        harness.close()


def test_utf8_message_round_trip_through_http_run_and_history() -> None:
    """The request boundary must preserve every code point before execution."""

    message = "中文 2026 Agent\n第二行：立即发布 / draft"

    def result_factory(kwargs: dict[str, Any]) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            task_id="task-utf8",
        )

    harness = _make_harness(result_factory)
    try:
        conversation_id = _create_conversation(harness.client)
        body = json.dumps(
            {"content": message, "timezone": "Asia/Shanghai"},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        response = harness.client.post(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            content=body,
            headers={
                "Authorization": "Bearer test-token",
                "Content-Type": "application/json; charset=utf-8",
            },
        )

        assert response.status_code == 202
        run_id = response.json()["run_id"]
        accepted = harness.run_store.get(run_id)
        assert accepted is not None
        assert accepted.payload["message"] == message
        assert harness.client.app.state.message_store[conversation_id][0]["content"] == message
        assert [ord(char) for char in accepted.payload["message"]] == [ord(char) for char in message]
        assert harness.adapter.calls == []

        _drive_one_claimed_run(harness, run_id)
        assert harness.adapter.calls[0]["message"] == message
        assert [ord(char) for char in harness.adapter.calls[0]["message"]] == [ord(char) for char in message]
    finally:
        harness.close()


def test_update_message_preserves_task_binding_through_durable_run() -> None:
    def result_factory(kwargs: dict[str, Any]) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            task_id="task-existing",
            plan_id="plan-update",
            execution_id="execution-update",
            execution_path="runtime",
        )

    harness = _make_harness(result_factory)
    try:
        conversation_id = _create_conversation(
            harness.client,
            active_task_id="task-existing",
        )
        response = harness.client.post(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            json={"content": "把刚才的帖子改短一点"},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert response.json()["status"] == RUN_ACCEPTED

        _drive_one_claimed_run(harness, run_id)

        assert harness.adapter.calls[0]["session"].active_task_id == "task-existing"
        assert harness.run_store.get(run_id).status == RUN_COMPLETED  # type: ignore[union-attr]
    finally:
        harness.close()


def test_runtime_failure_is_persisted_after_acceptance() -> None:
    def result_factory(kwargs: dict[str, Any]) -> RuntimeResult:
        return RuntimeResult(
            success=False,
            status="FAILED",
            run_id=kwargs["run_id"],
            task_id="task-failed",
            execution_id="execution-failed",
            execution_path="runtime",
            error_code="TOOL_ARGUMENT_VALIDATION_FAILED",
            error_message="content argument is invalid",
        )

    harness = _make_harness(result_factory)
    try:
        conversation_id = _create_conversation(harness.client)
        response = harness.client.post(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            json={"content": "写一篇文章"},
            headers={"Authorization": "Bearer test-token"},
        )

        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert response.json()["status"] == RUN_ACCEPTED

        _drive_one_claimed_run(harness, run_id)

        failed = harness.run_store.get(run_id)
        assert failed is not None
        assert failed.status == RUN_FAILED
        assert failed.error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
        assert failed.error_message == "content argument is invalid"
    finally:
        harness.close()


def test_missing_durable_run_store_fails_closed() -> None:
    harness = _make_harness(lambda kwargs: RuntimeResult(success=True, run_id=kwargs["run_id"]))
    try:
        harness.client.app.state.agent_run_store = None
        conversation_id = _create_conversation(harness.client)
        response = harness.client.post(
            f"/api/v1/agent/conversations/{conversation_id}/messages",
            json={"content": "写一篇文章"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert response.status_code == 503
        assert harness.adapter.calls == []
    finally:
        harness.close()


def _send(harness: _Harness, conv: str, token: str, content: str):
    return harness.client.post(
        f"/api/v1/agent/conversations/{conv}/messages",
        json={"content": content},
        headers={"Authorization": f"Bearer {token}"},
    )


def test_follow_up_never_crosses_user_or_conversation_scope() -> None:
    """Regression: a working Run of another user/conversation must never become
    the follow-up parent (a cross-scope link deadlocks the runner's follow-up
    serialization — observed live with two test accounts)."""
    def validator(token: str) -> AuthContext:
        user = token.removeprefix("Bearer ").strip() or "user-1"
        return AuthContext(user_id=user, tenant_id="tenant-1", raw_access_token=token)

    harness = _make_harness(
        lambda kwargs: RuntimeResult(success=True, run_id=kwargs["run_id"]),
        validator=validator,
    )
    try:
        # user-1 creates conversation A and a working Run.
        conv_a = harness.client.post(
            "/api/v1/agent/conversations",
            json={"title": "scope-a"},
            headers={"Authorization": "Bearer user-1"},
        ).json()["conversation_id"]
        first = _send(harness, conv_a, "user-1", "任务A")
        assert first.status_code == 202
        run_a = first.json()["run_id"]
        assert first.json().get("follow_up_of") is None

        # user-2 in a DIFFERENT conversation: must not queue behind user-1's Run.
        conv_b = harness.client.post(
            "/api/v1/agent/conversations",
            json={"title": "scope-b"},
            headers={"Authorization": "Bearer user-2"},
        ).json()["conversation_id"]
        second = _send(harness, conv_b, "user-2", "任务B")
        assert second.status_code == 202
        assert second.json().get("follow_up_of") is None, (
            "cross-user/cross-conversation follow-up link created"
        )

        # user-1 in the SAME conversation queues behind its own working Run.
        third = _send(harness, conv_a, "user-1", "补充A")
        assert third.status_code == 202
        assert third.json()["follow_up_of"] == run_a

        # A follow-up parent must resolve to an existing Run of the same scope.
        parent = harness.run_store.get(run_a)
        assert parent is not None and parent.user_id == "user-1"
    finally:
        harness.close()
