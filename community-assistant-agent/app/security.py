from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Header, HTTPException, status
from jwt import PyJWKClient

from app.config import get_settings
from app.domain import Principal


_jwks_client: PyJWKClient | None = None


def _client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(get_settings().identity_jwks_url, cache_keys=True)
    return _jwks_client


async def current_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "需要登录后使用知光助手")
    token = authorization[7:].strip()
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=get_settings().identity_audience,
            issuer=get_settings().identity_issuer,
        )
    except Exception as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "登录凭证无效或已过期"
        ) from exc
    if payload.get("token_type") != "access":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "必须使用 access token")
    role = str(payload.get("role", "USER"))
    if role != "USER":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "仅社区用户可以使用知光助手")
    return Principal(
        user_id=str(payload.get("uid") or payload.get("sub")),
        tenant_id=str(payload.get("tenant_id", "zhiguang")),
        role=role,
        display_name=str(payload.get("nickname", "知光用户")),
        token=token,
    )

