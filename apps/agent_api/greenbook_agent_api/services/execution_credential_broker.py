"""Process-local credential handoff for the API-managed queue consumer.

Bearer tokens are accepted only after the API authentication middleware has
validated them.  They are intentionally kept out of queue messages,
PostgreSQL, logs, checkpoints, and artifacts.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from typing import Any

from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.execution_queue_worker import (
    ExecutionHandlerDeferredError,
)
from greenbook_contracts.identity import AuthContext


class ExecutionCredentialBroker:
    """Keep the latest validated user credential for local queue execution."""

    def __init__(self, *, now_factory: Any = time.time) -> None:
        self._now = now_factory
        self._credentials: dict[tuple[str, str], tuple[AuthContext, float | None]] = {}
        self._lock = threading.RLock()

    def register(self, auth: AuthContext) -> None:
        """Register an identity that has already passed JWT validation."""

        key = self._key(auth.tenant_id, auth.user_id)
        expires_at = _jwt_expiry(auth.raw_access_token)
        with self._lock:
            self._credentials[key] = (auth.model_copy(deep=True), expires_at)

    def resolve(self, message: ExecutionQueueMessage) -> AuthContext:
        """Resolve the credential matching the immutable queued identity."""

        identity = message.payload.get("auth_context") or {}
        user_id = str(identity.get("user_id") or message.payload.get("user_id") or "")
        tenant_id = str(
            identity.get("tenant_id") or message.payload.get("tenant_id") or ""
        )
        if not user_id or not tenant_id:
            raise RuntimeError(
                f"Queued execution {message.execution_id} has no authenticated scope"
            )

        key = self._key(tenant_id, user_id)
        with self._lock:
            entry = self._credentials.get(key)
            if entry is None:
                raise ExecutionHandlerDeferredError("validated user credential unavailable")
            auth, expires_at = entry
            if expires_at is not None and expires_at <= float(self._now()):
                self._credentials.pop(key, None)
                raise ExecutionHandlerDeferredError("validated user credential expired")
            return auth.model_copy(
                update={
                    "timezone": str(
                        identity.get("timezone")
                        or message.payload.get("timezone")
                        or auth.timezone
                    )
                },
                deep=True,
            )

    @staticmethod
    def _key(tenant_id: str, user_id: str) -> tuple[str, str]:
        return (str(tenant_id), str(user_id))


def _jwt_expiry(token: str) -> float | None:
    """Read an expiry hint from a token already validated by middleware."""

    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        value = payload.get("exp")
        return float(value) if value is not None else None
    except (IndexError, TypeError, ValueError, json.JSONDecodeError):
        # Test validators and non-JWT development identities may intentionally
        # use opaque tokens. Their validator remains the trust boundary.
        return None


__all__ = ["ExecutionCredentialBroker"]
