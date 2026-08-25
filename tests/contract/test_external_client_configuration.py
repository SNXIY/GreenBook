"""Production configuration and boundary semantics for external clients."""

from __future__ import annotations

import pytest
from greenbook_java_client.client import JavaClient


@pytest.mark.asyncio
async def test_java_client_reads_real_runtime_configuration(monkeypatch) -> None:
    monkeypatch.setenv("GREENBOOK_JAVA_CONNECT_TIMEOUT_SECONDS", "1.25")
    monkeypatch.setenv("GREENBOOK_JAVA_READ_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("GREENBOOK_JAVA_VERIFY_TLS", "false")

    client = JavaClient.from_env(base_url="https://java.example")

    assert client._base_url == "https://java.example"
    assert client.http.timeout.connect == 1.25
    assert client.http.timeout.read == 7.0
    await client.close()
