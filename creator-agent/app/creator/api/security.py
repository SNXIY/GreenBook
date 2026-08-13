from __future__ import annotations

import base64
import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.core.config import Settings, get_settings
from app.creator.api.identity import (
    CreatorOidcIdentityProvider,
    CreatorTrustedProxyIdentityProvider,
    bearer_token,
)
from app.creator.api.models import CreatorApiPrincipal


async def current_creator(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreatorApiPrincipal:
    mode = settings.creator_identity_mode.strip().lower()
    if mode == "oidc":
        return await CreatorOidcIdentityProvider(settings).authenticate(
            bearer_token(request.headers.get("authorization", ""))
        )
    if mode == "trusted_proxy":
        return await CreatorTrustedProxyIdentityProvider(settings).authenticate(request)
    return _basic_creator(request, settings)


def _basic_creator(
    request: Request,
    settings: Settings,
) -> CreatorApiPrincipal:
    principal = configured_basic_creator(settings)
    username, password = _basic_credentials(request.headers.get("authorization", ""))
    valid_username = hmac.compare_digest(
        username.encode("utf-8"),
        settings.creator_basic_username.encode("utf-8"),
    )
    valid_password = hmac.compare_digest(
        password.encode("utf-8"),
        settings.creator_basic_password.encode("utf-8"),
    )
    if not valid_username or not valid_password:
        raise _basic_unauthorized()
    return principal


def configured_basic_creator(settings: Settings) -> CreatorApiPrincipal:
    if (
        not settings.creator_basic_username.strip()
        or not settings.creator_basic_password
    ):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Local Basic authentication is not configured",
        )
    roles = frozenset(
        role.strip() for role in settings.creator_basic_roles.split(",") if role.strip()
    )
    return CreatorApiPrincipal(
        tenant_id=settings.creator_api_tenant_id,
        creator_id=settings.creator_basic_creator_id,
        actor_id=settings.creator_basic_actor_id,
        display_name=(
            settings.creator_basic_display_name or settings.creator_basic_username
        ),
        roles=roles or frozenset({"CREATOR"}),
    )


def create_local_basic_session(
    settings: Settings,
) -> tuple[str, CreatorApiPrincipal]:
    principal = configured_basic_creator(settings)
    credentials = (
        f"{settings.creator_basic_username}:{settings.creator_basic_password}"
    ).encode()
    return base64.b64encode(credentials).decode("ascii"), principal


def _basic_credentials(authorization: str) -> tuple[str, str]:
    scheme, separator, encoded = authorization.partition(" ")
    if separator != " " or scheme.lower() != "basic" or not encoded.strip():
        raise _basic_unauthorized()
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _basic_unauthorized() from exc
    if not username:
        raise _basic_unauthorized()
    return username, password


def _basic_unauthorized() -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "Invalid Basic credentials",
        headers={"WWW-Authenticate": 'Basic realm="mindflow-creator"'},
    )
