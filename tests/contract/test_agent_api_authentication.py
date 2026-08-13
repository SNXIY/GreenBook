"""Authentication regression tests for the active Agent API boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from greenbook_security import jwt as security_jwt
from greenbook_security.jwt import validate_access_token
from jwt.algorithms import RSAAlgorithm

from apps.agent_api.greenbook_agent_api.main import (
    DEFAULT_AGENT_IDENTITY_AUDIENCE,
    create_app,
)


@pytest.fixture
def assistant_auth_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk["kid"] = "assistant-auth-test-key"

    async def fetch_jwks(_url: str, *, force: bool = False) -> dict[str, object]:
        return {"keys": [jwk]}

    monkeypatch.setattr(security_jwt, "fetch_jwks", fetch_jwks)

    async def validate(token: str):
        return await validate_access_token(
            token,
            jwks_url="http://test-jwks/.well-known/jwks.json",
            issuer="http://127.0.0.1:8080",
            audience=DEFAULT_AGENT_IDENTITY_AUDIENCE,
        )

    app = create_app(auth_validator=validate)
    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}
    return TestClient(app), private_key


def _user_token(private_key: object, audience: list[str]) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "http://127.0.0.1:8080",
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "sub": "74",
        "uid": 74,
        "jti": "assistant-auth-test-token",
        "sid": "assistant-auth-test-session",
        "token_type": "access",
        "tenant_id": "zhiguang",
        "roles": ["USER"],
    }
    return jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "assistant-auth-test-key"},
    )


def test_valid_user_token_lists_conversations(assistant_auth_client) -> None:
    client, private_key = assistant_auth_client
    token = _user_token(private_key, ["zhiguang-api", DEFAULT_AGENT_IDENTITY_AUDIENCE])

    response = client.get(
        "/api/v1/agent/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["items"] == []


def test_invalid_audience_is_rejected(assistant_auth_client) -> None:
    client, private_key = assistant_auth_client
    token = _user_token(private_key, ["zhiguang-api"])

    response = client.get(
        "/api/v1/agent/conversations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
