"""Typed Task-level Semantic Confirmation control contract."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.user_activity import SemanticConfirmationAction

from apps.agent_api.greenbook_agent_api.main import create_app
from greenbook_agent_core.task import InMemoryTaskRepository, TaskManager
from greenbook_agent_core.task.semantic_confirmation import confirmation_identity


def _auth(_token: str) -> AuthContext:
    return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="token")


class _ResumeRecorder:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def resume_task(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(status="COMPLETED")


def _app_with_pending_task():
    app = create_app(auth_validator=_auth)
    manager = TaskManager(InMemoryTaskRepository())
    task = _run(manager.create_task(
        conversation_id="c1",
        user_id="u1",
        tenant_id="t1",
        root_goal="two real writes",
    ))
    task = _run(manager.set_confirmation_pending(
        task.task_id,
        snapshot_hash="canonical-v1",
        resume_run_id="run-confirmation",
    ))
    recorder = _ResumeRecorder()
    app.state.task_manager = manager
    app.state.action_loop_executor = recorder
    app.state.conversation_store = {}
    return app, task, recorder


def _run(value):
    import asyncio

    return asyncio.run(value)


def _body(task, action: SemanticConfirmationAction = SemanticConfirmationAction.CONFIRM):
    return {
        "action": action.value,
        "confirmation_id": confirmation_identity(task),
        "expected_task_version": task.version,
        "expected_confirmation_version": task.confirmation_version,
    }


def test_confirm_is_typed_cas_and_duplicate_is_idempotent() -> None:
    app, task, recorder = _app_with_pending_task()
    client = TestClient(app)
    path = f"/api/v1/agent/tasks/{task.task_id}/semantic-confirmation"
    headers = {"Authorization": "Bearer token"}

    first = client.post(path, json=_body(task), headers=headers)
    assert first.status_code == 200
    assert first.json()["confirmation_state"] == "CONFIRMED"
    assert first.json()["idempotent"] is False
    assert first.json()["resume_queued"] is True
    assert len(recorder.calls) == 1
    assert recorder.calls[0]["command"] is None

    duplicate = client.post(path, json=_body(task), headers=headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["idempotent"] is True
    assert len(recorder.calls) == 1


def test_stale_confirmation_and_cancel_never_resume() -> None:
    app, task, recorder = _app_with_pending_task()
    client = TestClient(app)
    path = f"/api/v1/agent/tasks/{task.task_id}/semantic-confirmation"
    headers = {"Authorization": "Bearer token"}

    stale = _body(task)
    stale["expected_task_version"] += 1
    response = client.post(path, json=stale, headers=headers)
    assert response.status_code == 409
    assert recorder.calls == []

    cancelled = client.post(
        path,
        json=_body(task, SemanticConfirmationAction.CANCEL),
        headers=headers,
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["confirmation_state"] == "CANCELLED"
    assert recorder.calls == []


def test_modify_supersedes_version_without_routing_to_interpreter() -> None:
    app, task, recorder = _app_with_pending_task()
    client = TestClient(app)
    path = f"/api/v1/agent/tasks/{task.task_id}/semantic-confirmation"
    response = client.post(
        path,
        json=_body(task, SemanticConfirmationAction.MODIFY),
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["confirmation_state"] == "SUPERSEDED"
    assert payload["requires_new_compilation"] is True
    assert recorder.calls == []
