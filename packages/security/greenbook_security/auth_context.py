"""AuthContext dependency for FastAPI — extracts and validates JWT from Authorization header."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from greenbook_contracts.identity import AuthContext
from greenbook_security.jwt import JwtValidationError, validate_access_token

logger = logging.getLogger(__name__)


class AuthContextResolver:
    """FastAPI dependency that validates the Bearer token and returns AuthContext."""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
    ) -> None:
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience

    async def __call__(
        self,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ) -> AuthContext:
        token = _extract_bearer(authorization)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid Authorization header",
            )

        try:
            auth_ctx = await validate_access_token(
                token,
                jwks_url=self._jwks_url,
                issuer=self._issuer,
                audience=self._audience,
            )
        except JwtValidationError as exc:
            logger.warning("JWT validation failed: %s", exc)
            http_status = (
                status.HTTP_401_UNAUTHORIZED
                if exc.code == "AUTHENTICATION_REQUIRED"
                else status.HTTP_400_BAD_REQUEST
            )
            raise HTTPException(status_code=http_status, detail=str(exc)) from exc

        # Store on request state for downstream use
        request.state.auth_context = auth_ctx
        return auth_ctx


def _extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1]
    return None
