from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from service import app


@pytest.fixture
def task_list_runtime():
    previous = getattr(app.state, "moderation_services", None)
    workflow = SimpleNamespace(list_tasks=AsyncMock(return_value=[]))
    app.state.moderation_services = SimpleNamespace(workflow=workflow)
    yield workflow
    if previous is None:
        del app.state.moderation_services
    else:
        app.state.moderation_services = previous


def test_no_auth_secret(mock_settings, task_list_runtime, test_client):
    mock_settings.AUTH_SECRET = None

    with_header = test_client.get(
        "/moderation/tasks",
        headers={"Authorization": "Bearer any-token"},
    )
    without_header = test_client.get("/moderation/tasks")

    assert with_header.status_code == 200
    assert without_header.status_code == 200


def test_auth_secret_correct(mock_settings, task_list_runtime, test_client):
    mock_settings.AUTH_SECRET = SecretStr("test-secret")

    response = test_client.get(
        "/moderation/tasks",
        headers={"Authorization": "Bearer test-secret"},
    )

    assert response.status_code == 200


def test_auth_secret_incorrect(mock_settings, task_list_runtime, test_client):
    mock_settings.AUTH_SECRET = SecretStr("test-secret")

    wrong_token = test_client.get(
        "/moderation/tasks",
        headers={"Authorization": "Bearer wrong-secret"},
    )
    missing_token = test_client.get("/moderation/tasks")

    assert wrong_token.status_code == 401
    assert missing_token.status_code == 401
