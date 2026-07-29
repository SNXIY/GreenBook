from __future__ import annotations

import base64
import hashlib
import hmac
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from starlette.requests import Request

from app.core.config import Settings
from app.creator.api.identity import (
    CreatorOidcIdentityProvider,
    CreatorTrustedProxyIdentityProvider,
    bearer_token,
    validate_creator_identity_settings,
)
from app.creator.api.security import _basic_creator


class _StaticSigningKeys:
    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _: str) -> SimpleNamespace:
        return SimpleNamespace(key=self._public_key)


class CreatorIdentityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        self.public_key = self.private_key.public_key()
        self.settings = Settings(
            _env_file=None,
            creator_identity_mode="oidc",
            creator_identity_issuer="https://identity.example.test",
            creator_identity_audience="mindflow-creator",
            creator_identity_jwks_url=(
                "https://identity.example.test/.well-known/jwks.json"
            ),
            creator_identity_algorithms="RS256",
            creator_identity_required_role="CREATOR",
        )
        self.provider = CreatorOidcIdentityProvider(
            self.settings,
            signing_keys=_StaticSigningKeys(self.public_key),
        )

    async def test_signed_claims_define_creator_scope(self) -> None:
        principal = await self.provider.authenticate(self._token())

        self.assertEqual(principal.tenant_id, "tenant-platform")
        self.assertEqual(principal.creator_id, "creator-platform")
        self.assertEqual(principal.actor_id, "user-platform-42")
        self.assertEqual(principal.display_name, "Platform Creator")
        self.assertEqual(principal.roles, frozenset({"CREATOR", "EDITOR"}))

    async def test_invalid_audience_is_rejected_without_details(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self.provider.authenticate(self._token(audience="another-service"))

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(
            raised.exception.headers,
            {"WWW-Authenticate": "Bearer"},
        )
        self.assertNotIn("audience", str(raised.exception.detail).lower())

    async def test_missing_creator_role_is_forbidden(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await self.provider.authenticate(self._token(roles=["EDITOR"]))

        self.assertEqual(raised.exception.status_code, 403)

    def test_unsafe_oidc_configuration_is_rejected(self) -> None:
        symmetric = self.settings.model_copy(
            update={"creator_identity_algorithms": "HS256"}
        )
        with self.assertRaises(ValueError):
            validate_creator_identity_settings(symmetric)

        insecure = self.settings.model_copy(
            update={
                "creator_identity_jwks_url": (
                    "http://identity.example.test/.well-known/jwks.json"
                )
            }
        )
        with self.assertRaises(ValueError):
            validate_creator_identity_settings(insecure)

    def test_bearer_scheme_is_strict(self) -> None:
        self.assertEqual(bearer_token("Bearer signed-token"), "signed-token")
        with self.assertRaises(HTTPException):
            bearer_token("Basic signed-token")

    def test_creator_basic_auth_does_not_require_legacy_user_storage(self) -> None:
        settings = Settings(
            _env_file=None,
            creator_identity_mode="basic",
            creator_api_tenant_id="tenant-local",
            creator_basic_username="creator",
            creator_basic_password="local-secret",
            creator_basic_creator_id="creator-local",
            creator_basic_actor_id="actor-local",
            creator_basic_display_name="Local Creator",
        )
        encoded = base64.b64encode(b"creator:local-secret").decode("ascii")
        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", f"Basic {encoded}".encode("ascii"))],
            }
        )

        principal = _basic_creator(request, settings)

        self.assertEqual(principal.tenant_id, "tenant-local")
        self.assertEqual(principal.creator_id, "creator-local")
        self.assertEqual(principal.actor_id, "actor-local")
        self.assertEqual(principal.roles, frozenset({"CREATOR"}))

    def test_creator_basic_auth_fails_closed_without_password(self) -> None:
        settings = Settings(
            _env_file=None,
            creator_identity_mode="basic",
            creator_basic_username="creator",
            creator_basic_password="",
        )
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "scheme": "http",
                "path": "/api/v1/creator/status",
                "headers": [],
            }
        )

        with self.assertRaises(HTTPException) as raised:
            _basic_creator(request, settings)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_trusted_proxy_signature_binds_java_user_identity(self) -> None:
        settings = Settings(
            _env_file=None,
            creator_identity_mode="trusted_proxy",
            creator_trusted_proxy_shared_secret="integration-secret",
            creator_trusted_proxy_allowed_service="zhiguang-java-backend",
            creator_trusted_proxy_tenant_id="zhiguang",
        )
        request = _trusted_proxy_request(
            secret="integration-secret",
            body=b'{"goal":"creator integration"}',
        )

        principal = await CreatorTrustedProxyIdentityProvider(settings).authenticate(
            request
        )

        self.assertEqual(principal.tenant_id, "zhiguang")
        self.assertEqual(principal.creator_id, "42")
        self.assertEqual(principal.actor_id, "42")
        self.assertEqual(principal.roles, frozenset({"CREATOR", "USER"}))

    async def test_trusted_proxy_rejects_replayed_nonce(self) -> None:
        settings = Settings(
            _env_file=None,
            creator_identity_mode="trusted_proxy",
            creator_trusted_proxy_shared_secret="integration-secret",
            creator_trusted_proxy_allowed_service="zhiguang-java-backend",
            creator_trusted_proxy_tenant_id="zhiguang",
        )
        nonce = uuid.uuid4().hex
        provider = CreatorTrustedProxyIdentityProvider(settings)

        await provider.authenticate(
            _trusted_proxy_request(
                secret="integration-secret",
                body=b"{}",
                nonce=nonce,
            )
        )
        with self.assertRaises(HTTPException) as raised:
            await provider.authenticate(
                _trusted_proxy_request(
                    secret="integration-secret",
                    body=b"{}",
                    nonce=nonce,
                )
            )

        self.assertEqual(raised.exception.status_code, 409)

    async def test_trusted_proxy_signature_binds_query_string(self) -> None:
        settings = Settings(
            _env_file=None,
            creator_identity_mode="trusted_proxy",
            creator_trusted_proxy_shared_secret="integration-secret",
            creator_trusted_proxy_allowed_service="zhiguang-java-backend",
            creator_trusted_proxy_tenant_id="zhiguang",
        )
        provider = CreatorTrustedProxyIdentityProvider(settings)

        with self.assertRaises(HTTPException) as raised:
            await provider.authenticate(
                _trusted_proxy_request(
                    secret="integration-secret",
                    body=b"",
                    method="GET",
                    query_string="limit=50",
                    signed_query_string="limit=20",
                )
            )

        self.assertEqual(raised.exception.status_code, 401)

    def test_trusted_proxy_requires_fail_closed_configuration(self) -> None:
        settings = Settings(
            _env_file=None,
            creator_identity_mode="trusted_proxy",
            creator_trusted_proxy_shared_secret="",
        )
        with self.assertRaises(ValueError):
            validate_creator_identity_settings(settings)

    def _token(
        self,
        *,
        audience: str = "mindflow-creator",
        roles: list[str] | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        return jwt.encode(
            {
                "iss": "https://identity.example.test",
                "aud": audience,
                "sub": "user-platform-42",
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "tenant_id": "tenant-platform",
                "creator_id": "creator-platform",
                "roles": roles or ["CREATOR", "EDITOR"],
                "name": "Platform Creator",
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "creator-test-key"},
        )


def _trusted_proxy_request(
    *,
    secret: str,
    body: bytes,
    nonce: str | None = None,
    method: str = "POST",
    query_string: str = "",
    signed_query_string: str | None = None,
) -> Request:
    path = "/api/v1/creator/tasks"
    signed_target = path
    signature_query = (
        query_string if signed_query_string is None else signed_query_string
    )
    if signature_query:
        signed_target = f"{path}?{signature_query}"
    timestamp = str(int(time.time()))
    actual_nonce = nonce or uuid.uuid4().hex
    user_id = "42"
    roles = "CREATOR,USER"
    trace_id = str(uuid.uuid4())
    canonical = "\n".join(
        (
            method,
            signed_target,
            timestamp,
            actual_nonce,
            user_id,
            roles,
            trace_id,
            hashlib.sha256(body).hexdigest(),
        )
    )
    signature = hmac.new(
        secret.encode(),
        canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "x-zhiguang-service": "zhiguang-java-backend",
        "x-zhiguang-user-id": user_id,
        "x-zhiguang-roles": roles,
        "x-trace-id": trace_id,
        "x-zhiguang-timestamp": timestamp,
        "x-zhiguang-nonce": actual_nonce,
        "x-zhiguang-signature": signature,
    }
    delivered = False

    async def receive() -> dict[str, object]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query_string.encode(),
            "headers": [
                (name.encode(), value.encode()) for name, value in headers.items()
            ],
            "client": ("127.0.0.1", 1),
            "server": ("creator.test", 80),
        },
        receive,
    )


if __name__ == "__main__":
    unittest.main()
