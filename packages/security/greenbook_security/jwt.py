"""RS256 JWT verification against the Java-issued JWKS endpoint.

Validates: alg=RS256, kid, signature, issuer, audience, exp, nbf,
token_type=access, uid, tenant_id, roles.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import jwt
from greenbook_contracts.identity import AuthContext

logger = logging.getLogger(__name__)

_JWKS_CACHE: dict[str, Any] = {}
_JWKS_CACHE_TS: float = 0.0
_JWKS_TTL_SECONDS: float = 300.0  # 5 minutes


class JwtValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "VALIDATION_ERROR") -> None:
        self.code = code
        super().__init__(message)


async def fetch_jwks(jwks_url: str, *, force: bool = False) -> dict[str, Any]:
    """Fetch JWKS from Java, cached in memory with configurable TTL."""
    global _JWKS_CACHE, _JWKS_CACHE_TS

    now = time.monotonic()
    if not force and _JWKS_CACHE and (now - _JWKS_CACHE_TS) < _JWKS_TTL_SECONDS:
        return _JWKS_CACHE

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        resp = await client.get(jwks_url)
        resp.raise_for_status()
        data = resp.json()
        if "keys" not in data:
            raise JwtValidationError("JWKS response missing 'keys'", code="INTERNAL_ERROR")

    _JWKS_CACHE = data
    _JWKS_CACHE_TS = now
    return data


def _find_key(jwks: dict[str, Any], kid: str) -> dict[str, Any] | None:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def _build_public_key(jwk: dict[str, Any]) -> str:
    from jwt.algorithms import RSAAlgorithm
    return RSAAlgorithm.from_jwk(jwk)


async def validate_access_token(
    token: str,
    *,
    jwks_url: str,
    issuer: str,
    audience: str,
) -> AuthContext:
    """Validate RS256 access token and return AuthContext.

    Rejects: refresh tokens, wrong issuer, wrong audience, expired tokens,
    unknown kid, non-RS256 tokens, missing uid, missing token_type.
    """
    # Decode header without verification to get kid
    try:
        unverified = jwt.api_jwt.decode_complete(
            token, options={"verify_signature": False}
        )
    except Exception as exc:
        raise JwtValidationError(f"Malformed JWT: {exc}") from exc

    header = unverified.get("header", {})
    kid = header.get("kid")
    alg = header.get("alg", "").upper()

    if not kid:
        raise JwtValidationError("JWT missing 'kid' in header")

    if alg != "RS256":
        raise JwtValidationError(f"Unsupported algorithm: {alg}. Only RS256 accepted.")

    # Fetch JWKS
    jwks = await fetch_jwks(jwks_url)
    jwk = _find_key(jwks, kid)

    # Unknown kid → force refresh once
    if jwk is None:
        jwks = await fetch_jwks(jwks_url, force=True)
        jwk = _find_key(jwks, kid)
        if jwk is None:
            raise JwtValidationError(
                f"Unknown kid '{kid}' in JWT header. JWKS refresh did not find it."
            )

    public_key = _build_public_key(jwk)

    try:
        payload = jwt.decode(
            token,
            key=public_key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"verify_exp": True, "verify_nbf": True},
        )
    except jwt.ExpiredSignatureError as exc:
        raise JwtValidationError("Access token has expired", code="AUTHENTICATION_REQUIRED") from exc
    except jwt.InvalidAudienceError as exc:
        raise JwtValidationError(
            f"Audience mismatch: expected '{audience}'", code="AUTHENTICATION_REQUIRED"
        ) from exc
    except jwt.InvalidIssuerError as exc:
        raise JwtValidationError(
            f"Issuer mismatch: expected '{issuer}'", code="AUTHENTICATION_REQUIRED"
        ) from exc
    except jwt.ImmatureSignatureError as exc:
        raise JwtValidationError("Token not yet valid (nbf)", code="AUTHENTICATION_REQUIRED") from exc
    except Exception as exc:
        raise JwtValidationError(f"JWT verification failed: {exc}", code="AUTHENTICATION_REQUIRED") from exc

    # Application-level claims
    token_type = payload.get("token_type", "")
    if token_type != "access":
        raise JwtValidationError(
            f"token_type must be 'access', got '{token_type}'"
        )

    uid = payload.get("uid") or payload.get("sub") or ""
    if not uid:
        raise JwtValidationError("JWT missing 'uid' claim")

    tenant_id = payload.get("tenant_id") or ""
    if not tenant_id:
        raise JwtValidationError("JWT missing 'tenant_id' claim")

    roles = payload.get("roles", [])
    if isinstance(roles, str):
        roles = [r.strip() for r in roles.split(",") if r.strip()]
    if not isinstance(roles, list):
        roles = []

    return AuthContext(
        user_id=uid,
        tenant_id=tenant_id,
        roles=roles,
        session_id=payload.get("sid"),
        token_id=payload.get("jti"),
        raw_access_token=token,
        timezone=payload.get("timezone", "Asia/Shanghai"),
    )
