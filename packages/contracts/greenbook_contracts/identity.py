from __future__ import annotations

from pydantic import BaseModel, Field


class AuthContext(BaseModel):
    """Verified user identity from JWT — never set or modified by the model.

    raw_access_token is only used for Java Client token relay and must never
    enter logs, database, or model context windows.
    """

    user_id: str
    tenant_id: str
    roles: list[str] = Field(default_factory=list)
    session_id: str | None = None
    token_id: str | None = None
    raw_access_token: str = Field(repr=False, exclude=True)
    timezone: str = "Asia/Shanghai"
