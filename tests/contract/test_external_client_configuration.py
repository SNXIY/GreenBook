"""Production configuration and boundary semantics for external clients."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from greenbook_creator_client.client import CreatorClient
from greenbook_java_client.client import JavaClient


@pytest.mark.asyncio
async def test_java_client_reads_real_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setenv("ASSISTANT_JAVA_CONNECT_TIMEOUT_SECONDS", "1.25")
    monkeypatch.setenv("ASSISTANT_JAVA_READ_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("ASSISTANT_JAVA_VERIFY_TLS", "false")

    client = JavaClient.from_env(base_url="https://java.example")

    assert client._base_url == "https://java.example"
    assert client.http.timeout.connect == 1.25
    assert client.http.timeout.read == 7.0
    await client.close()


def test_creator_client_reads_real_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setenv("ASSISTANT_CREATOR_TIMEOUT_SECONDS", "18")
    monkeypatch.setenv("ASSISTANT_CREATOR_POLL_INTERVAL_SECONDS", "0.5")

    client = CreatorClient.from_env(base_url="https://creator.example")

    assert client._base_url == "https://creator.example"
    assert client._poll_interval == 0.5
    assert client.http.timeout.read == 18.0


@pytest.mark.asyncio
async def test_creator_write_read_timeout_preserves_unknown_delivery() -> None:
    client = CreatorClient(base_url="http://creator.example")
    client.http.post = AsyncMock(side_effect=httpx.ReadTimeout("read timeout"))

    result = await client.create_task(kind="CREATE_CONTENT", goal="write")

    assert result.code == "RESULT_UNKNOWN"
    assert result.request_sent is True
    await client.close()


@pytest.mark.asyncio
async def test_creator_invalid_audience_is_auth_failure_with_sent_evidence() -> None:
    client = CreatorClient(base_url="http://creator.example")
    response = SimpleNamespace(
        status_code=401,
        text="invalid_audience",
        headers={},
        json=lambda: {"error": {"code": "invalid_audience"}},
    )
    client.http.post = AsyncMock(return_value=response)

    result = await client.create_task(kind="CREATE_CONTENT", goal="write")

    assert result.code == "AUTHENTICATION_FAILED"
    assert result.request_sent is True
    await client.close()
