from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import threading
import time
from functools import lru_cache
from typing import Any, Protocol
from urllib.parse import urlparse

import jwt
from fastapi import HTTPException, Request, status
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientError, PyJWTError

from app.creator.api.models import CreatorApiPrincipal


logger = logging.getLogger(__name__)

_ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


class CreatorIdentitySettings(Protocol):
    creator_identity_mode: str
    creator_identity_issuer: str
    creator_identity_audience: str
    creator_identity_jwks_url: str
    creator_identity_algorithms: str
    creator_identity_tenant_claim: str
    creator_identity_creator_claim: str
    creator_identity_roles_claim: str
    creator_identity_display_name_claim: str
    creator_identity_required_role: str
    creator_identity_leeway_seconds: float
    creator_identity_jwks_cache_seconds: float
    creator_identity_jwks_timeout_seconds: float
    creator_identity_allow_insecure_http: bool
    creator_trusted_proxy_shared_secret: str
    creator_trusted_proxy_allowed_service: str
    creator_trusted_proxy_tenant_id: str
    creator_trusted_proxy_required_role: str
    creator_trusted_proxy_allowed_skew_seconds: int
    creator_trusted_proxy_nonce_ttl_seconds: int


class CreatorSigningKeyClient(Protocol):
    def get_signing_key_from_jwt(self, token: str) -> Any: ...


class CreatorOidcIdentityProvider:
    def __init__(
        self,
        settings: CreatorIdentitySettings,
        *,
        signing_keys: CreatorSigningKeyClient | None = None,
    ) -> None:
        validate_creator_identity_settings(settings)
        self._settings = settings
        self._algorithms = _configured_algorithms(settings)
        self._signing_keys = signing_keys or _jwks_client(
            settings.creator_identity_jwks_url,
            settings.creator_identity_jwks_cache_seconds,
            settings.creator_identity_jwks_timeout_seconds,
        )

    async def authenticate(self, token: str) -> CreatorApiPrincipal:
        if not token:
            raise _unauthorized()
        try:
            signing_key = await asyncio.to_thread(
                self._signing_keys.get_signing_key_from_jwt,
                token,
            )
            required_claims = [
                "exp",
                "iat",
                "iss",
                "sub",
                self._settings.creator_identity_tenant_claim,
                self._settings.creator_identity_creator_claim,
                self._settings.creator_identity_roles_claim,
            ]
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._algorithms),
                audience=self._settings.creator_identity_audience,
                issuer=self._settings.creator_identity_issuer,
                leeway=max(
                    0.0,
                    self._settings.creator_identity_leeway_seconds,
                ),
                options={"require": required_claims},
            )
        except (PyJWTError, PyJWKClientError) as exc:
            logger.warning(
                "Creator OIDC authentication rejected error_type=%s",
                type(exc).__name__,
            )
            raise _unauthorized() from exc

        tenant_id = _required_text_claim(
            claims,
            self._settings.creator_identity_tenant_claim,
        )
        creator_id = _required_text_claim(
            claims,
            self._settings.creator_identity_creator_claim,
        )
        actor_id = _required_text_claim(claims, "sub")
        roles = _roles_claim(
            claims,
            self._settings.creator_identity_roles_claim,
        )
        required_role = self._settings.creator_identity_required_role.strip()
        if required_role and required_role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Creator role is required",
            )
        display_name = _optional_text_claim(
            claims,
            self._settings.creator_identity_display_name_claim,
        )
        return CreatorApiPrincipal(
            tenant_id=tenant_id,
            creator_id=creator_id,
            actor_id=actor_id,
            display_name=display_name or creator_id,
            roles=frozenset(roles),
        )


class CreatorTrustedProxyIdentityProvider:
    """Authenticates identity assertions signed by the Zhiguang Java gateway."""

    _MAX_BODY_BYTES = 1_048_576

    def __init__(self, settings: CreatorIdentitySettings) -> None:
        validate_creator_identity_settings(settings)
        self._settings = settings
        self._nonces = _trusted_proxy_nonce_guard(
            settings.creator_trusted_proxy_nonce_ttl_seconds
        )

    async def authenticate(self, request: Request) -> CreatorApiPrincipal:
        body = await request.body()
        if len(body) > self._MAX_BODY_BYTES:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                "Trusted proxy request body is too large",
            )

        service = _required_header(request, "X-Zhiguang-Service")
        user_id = _required_header(request, "X-Zhiguang-User-Id")
        raw_roles = _required_header(request, "X-Zhiguang-Roles")
        trace_id = _required_header(request, "X-Trace-Id")
        timestamp = _required_header(request, "X-Zhiguang-Timestamp")
        nonce = _required_header(request, "X-Zhiguang-Nonce")
        supplied = _required_header(request, "X-Zhiguang-Signature")

        if not hmac.compare_digest(
            service,
            self._settings.creator_trusted_proxy_allowed_service,
        ):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Trusted proxy service is not allowed",
            )
        if (
            len(user_id) > 128
            or len(trace_id) > 128
            or len(raw_roles) > 1_024
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", nonce)
            or not re.fullmatch(r"[0-9a-fA-F]{64}", supplied)
        ):
            raise _trusted_proxy_unauthorized()

        try:
            requested_at = int(timestamp)
        except ValueError as exc:
            raise _trusted_proxy_unauthorized() from exc
        if (
            abs(int(time.time()) - requested_at)
            > self._settings.creator_trusted_proxy_allowed_skew_seconds
        ):
            raise _trusted_proxy_unauthorized()

        expected = _trusted_proxy_signature(
            self._settings.creator_trusted_proxy_shared_secret,
            method=request.method,
            path=_request_target(request),
            timestamp=timestamp,
            nonce=nonce,
            user_id=user_id,
            roles=raw_roles,
            trace_id=trace_id,
            body=body,
        )
        if not hmac.compare_digest(expected, supplied.lower()):
            raise _trusted_proxy_unauthorized()
        if not self._nonces.consume(nonce):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Trusted proxy request was already used",
            )

        roles = frozenset(
            role.strip().upper() for role in raw_roles.split(",") if role.strip()
        )
        required_role = (
            self._settings.creator_trusted_proxy_required_role.strip().upper()
        )
        if required_role and required_role not in roles:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Creator role is required",
            )
        return CreatorApiPrincipal(
            tenant_id=self._settings.creator_trusted_proxy_tenant_id,
            creator_id=user_id,
            actor_id=user_id,
            display_name=user_id,
            roles=roles,
        )


def validate_creator_identity_settings(
    settings: CreatorIdentitySettings,
) -> None:
    mode = settings.creator_identity_mode.strip().lower()
    if mode not in {"basic", "oidc", "trusted_proxy"}:
        raise ValueError(
            "CREATOR_IDENTITY_MODE must be 'basic', 'oidc', or 'trusted_proxy'"
        )
    if mode == "basic":
        return
    if mode == "trusted_proxy":
        required_values = {
            "CREATOR_TRUSTED_PROXY_SHARED_SECRET": (
                settings.creator_trusted_proxy_shared_secret
            ),
            "CREATOR_TRUSTED_PROXY_ALLOWED_SERVICE": (
                settings.creator_trusted_proxy_allowed_service
            ),
            "CREATOR_TRUSTED_PROXY_TENANT_ID": (
                settings.creator_trusted_proxy_tenant_id
            ),
        }
        missing = sorted(
            name for name, value in required_values.items() if not value.strip()
        )
        if missing:
            raise ValueError(
                "Trusted proxy Creator identity is missing: " + ", ".join(missing)
            )
        if settings.creator_trusted_proxy_allowed_skew_seconds <= 0:
            raise ValueError("Trusted proxy clock skew must be positive")
        if settings.creator_trusted_proxy_nonce_ttl_seconds <= 0:
            raise ValueError("Trusted proxy nonce lifetime must be positive")
        return
    required_values = {
        "CREATOR_IDENTITY_ISSUER": settings.creator_identity_issuer,
        "CREATOR_IDENTITY_AUDIENCE": settings.creator_identity_audience,
        "CREATOR_IDENTITY_JWKS_URL": settings.creator_identity_jwks_url,
        "CREATOR_IDENTITY_TENANT_CLAIM": (settings.creator_identity_tenant_claim),
        "CREATOR_IDENTITY_CREATOR_CLAIM": (settings.creator_identity_creator_claim),
        "CREATOR_IDENTITY_ROLES_CLAIM": settings.creator_identity_roles_claim,
    }
    missing = sorted(
        name for name, value in required_values.items() if not value.strip()
    )
    if missing:
        raise ValueError("OIDC Creator identity is missing: " + ", ".join(missing))
    _configured_algorithms(settings)
    for name, value in (
        ("CREATOR_IDENTITY_ISSUER", settings.creator_identity_issuer),
        ("CREATOR_IDENTITY_JWKS_URL", settings.creator_identity_jwks_url),
    ):
        parsed = urlparse(value)
        secure = parsed.scheme == "https" and bool(parsed.netloc)
        explicitly_insecure = (
            settings.creator_identity_allow_insecure_http
            and parsed.scheme == "http"
            and bool(parsed.netloc)
        )
        if not secure and not explicitly_insecure:
            raise ValueError(
                f"{name} must use HTTPS unless insecure HTTP is explicitly enabled"
            )
    if settings.creator_identity_jwks_cache_seconds <= 0:
        raise ValueError("Creator JWKS cache lifetime must be positive")
    if settings.creator_identity_jwks_timeout_seconds <= 0:
        raise ValueError("Creator JWKS timeout must be positive")


def bearer_token(authorization: str) -> str:
    scheme, separator, credentials = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not credentials.strip():
        raise _unauthorized()
    return credentials.strip()


def _configured_algorithms(
    settings: CreatorIdentitySettings,
) -> tuple[str, ...]:
    algorithms = tuple(
        part.strip()
        for part in settings.creator_identity_algorithms.split(",")
        if part.strip()
    )
    if not algorithms or any(
        algorithm not in _ASYMMETRIC_ALGORITHMS for algorithm in algorithms
    ):
        raise ValueError(
            "Creator OIDC algorithms must be an explicit non-empty subset of "
            f"{sorted(_ASYMMETRIC_ALGORITHMS)}"
        )
    return algorithms


@lru_cache(maxsize=16)
def _jwks_client(
    url: str,
    cache_seconds: float,
    timeout_seconds: float,
) -> PyJWKClient:
    return PyJWKClient(
        url,
        cache_keys=False,
        cache_jwk_set=True,
        lifespan=cache_seconds,
        timeout=timeout_seconds,
    )


def _required_text_claim(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise _unauthorized()
    return value.strip()


def _optional_text_claim(claims: dict[str, Any], name: str) -> str | None:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _unauthorized()
    return value.strip() or None


def _roles_claim(claims: dict[str, Any], name: str) -> frozenset[str]:
    value = claims.get(name)
    if isinstance(value, str):
        roles = {
            role.strip() for role in value.replace(",", " ").split() if role.strip()
        }
    elif isinstance(value, list) and all(isinstance(role, str) for role in value):
        roles = {role.strip() for role in value if role.strip()}
    else:
        raise _unauthorized()
    if not roles:
        raise _unauthorized()
    return frozenset(roles)


class _TrustedProxyNonceGuard:
    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._values: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, nonce: str) -> bool:
        now = time.monotonic()
        with self._lock:
            expired = [
                value for value, expires_at in self._values.items() if expires_at <= now
            ]
            for value in expired:
                self._values.pop(value, None)
            if nonce in self._values:
                return False
            self._values[nonce] = now + self._ttl_seconds
            return True


@lru_cache(maxsize=8)
def _trusted_proxy_nonce_guard(ttl_seconds: int) -> _TrustedProxyNonceGuard:
    return _TrustedProxyNonceGuard(ttl_seconds)


def _required_header(request: Request, name: str) -> str:
    value = request.headers.get(name, "").strip()
    if not value:
        raise _trusted_proxy_unauthorized()
    return value


def _request_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    if isinstance(raw_path, bytes):
        path = raw_path.decode("latin-1")
    else:
        path = request.url.path
    raw_query = request.scope.get("query_string", b"")
    if isinstance(raw_query, bytes) and raw_query:
        return f"{path}?{raw_query.decode('latin-1')}"
    return path


def _trusted_proxy_signature(
    secret: str,
    *,
    method: str,
    path: str,
    timestamp: str,
    nonce: str,
    user_id: str,
    roles: str,
    trace_id: str,
    body: bytes,
) -> str:
    canonical = "\n".join(
        (
            method.upper(),
            path,
            timestamp,
            nonce,
            user_id,
            roles,
            trace_id,
            hashlib.sha256(body).hexdigest(),
        )
    )
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _trusted_proxy_unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid trusted proxy credentials",
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid bearer credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
