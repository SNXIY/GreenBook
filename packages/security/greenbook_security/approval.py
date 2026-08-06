"""Approval model bound to user, conversation, run, operation, and resource."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field


class Approval(BaseModel):
    approval_id: str
    user_id: str
    conversation_id: str
    run_id: str
    operation: str
    resource_id: str | None = None
    request_hash: str
    description: str = ""
    preview: dict[str, Any] = Field(default_factory=dict)
    expires_at: datetime
    status: str = "PENDING"

    @classmethod
    def create(
        cls,
        *,
        approval_id: str,
        user_id: str,
        conversation_id: str,
        run_id: str,
        operation: str,
        resource_id: str | None = None,
        description: str = "",
        preview: dict[str, Any] | None = None,
        ttl_minutes: int = 30,
    ) -> Approval:
        request_data = {
            "user_id": user_id,
            "conversation_id": conversation_id,
            "run_id": run_id,
            "operation": operation,
            "resource_id": resource_id,
        }
        request_hash = hashlib.sha256(
            json.dumps(request_data, sort_keys=True).encode()
        ).hexdigest()[:16]

        return cls(
            approval_id=approval_id,
            user_id=user_id,
            conversation_id=conversation_id,
            run_id=run_id,
            operation=operation,
            resource_id=resource_id,
            request_hash=request_hash,
            description=description,
            preview=preview or {},
            expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
        )

    def is_expired(self) -> bool:
        return datetime.now(UTC) > self.expires_at

    def is_valid_for(
        self,
        *,
        user_id: str,
        conversation_id: str,
        run_id: str,
        operation: str,
        resource_id: str | None = None,
    ) -> bool:
        if self.status != "APPROVED":
            return False
        if self.is_expired():
            return False
        if self.user_id != user_id:
            return False
        if self.conversation_id != conversation_id:
            return False
        if self.run_id != run_id:
            return False
        if self.operation != operation:
            return False
        return not (
            self.resource_id is not None
            and resource_id is not None
            and self.resource_id != resource_id
        )
