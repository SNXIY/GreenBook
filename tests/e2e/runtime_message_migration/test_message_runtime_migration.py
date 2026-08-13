"""E2E contract tests for the feature-flagged Runtime message entry."""

from __future__ import annotations

from typing import Callable

import pytest
from greenbook_contracts.identity import AuthContext
from starlette.testclient import TestClient

from apps.agent_api.greenbook_agent_api.main import create_app
from apps.agent_api.greenbook_agent_api.models.runtime_result import (
    RuntimeResult,
)
from greenbook_agent_core.compatibility.history import RunExecutionAdapter


class _RecordingRuntimeAdapter:
    def __init__(self, result_factory: Callable[[dict], RuntimeResult]) -> None:
        self.result_factory = result_factory
        self.calls: list[dict] = []

    async def execute(self, **kwargs) -> RuntimeResult:
        self.calls.append(kwargs)
        return self.result_factory(kwargs)


def _make_client(
    result_factory: Callable[[dict], RuntimeResult],
) -> tuple[TestClient, _RecordingRuntimeAdapter]:
    def validate(token: str) -> AuthContext:
        return AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token=token)

    app = create_app(auth_validator=validate)
    app.state.runtime_enabled = True
    app.state.execution_mode = "runtime"
    adapter = _RecordingRuntimeAdapter(result_factory)
    app.state.conversation_runtime_adapter = adapter
    app.state.run_execution_adapter = RunExecutionAdapter()
    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}
    return TestClient(app), adapter


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


@pytest.mark.parametrize("message", ["帮我写一篇AI Agent学习路线帖子"])
def test_create_content_message_uses_runtime_and_returns_execution_id(message: str) -> None:
    def result_factory(kwargs: dict) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            task_id="task-create",
            plan_id="plan-create",
            execution_id="execution-create",
            execution_path="runtime",
        )

    client, adapter = _make_client(result_factory)
    conversation_id = _create_conversation(client)

    response = client.post(
        f"/api/v1/agent/conversations/{conversation_id}/messages",
        json={"content": message},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["run_id"]
    assert payload["run_id"] != payload["execution_id"]
    assert payload["execution_id"] == "execution-create"
    assert payload["status"] == "COMPLETED"
    assert adapter.calls[0]["message"] == message
    assert client.app.state.run_store[payload["run_id"]]["execution_id"] == "execution-create"


def test_update_message_keeps_existing_task_binding_in_runtime_request() -> None:
    def result_factory(kwargs: dict) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=kwargs["run_id"],
            task_id="task-existing",
            plan_id="plan-update",
            execution_id="execution-update",
            execution_path="runtime",
        )

    client, adapter = _make_client(result_factory)
    conversation_id = _create_conversation(client, active_task_id="task-existing")

    response = client.post(
        f"/api/v1/agent/conversations/{conversation_id}/messages",
        json={"content": "把刚才的帖子改短一点"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 202
    assert adapter.calls[0]["session"].active_task_id == "task-existing"
    assert response.json()["execution_id"] == "execution-update"


def test_runtime_failure_keeps_error_code_and_execution_id() -> None:
    def result_factory(kwargs: dict) -> RuntimeResult:
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

    client, _adapter = _make_client(result_factory)
    conversation_id = _create_conversation(client)

    response = client.post(
        f"/api/v1/agent/conversations/{conversation_id}/messages",
        json={"content": "写一篇文章"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "FAILED"
    assert payload["error_code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert payload["execution_id"] == "execution-failed"
    assert payload["run_id"]
    assert client.app.state.run_store[payload["run_id"]]["status"] == "FAILED"
