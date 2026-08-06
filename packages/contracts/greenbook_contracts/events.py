from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BusinessEvent(BaseModel):
    """Async business event emitted to Kafka."""

    event_type: str
    event_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    trace_id: str | None = None
    conversation_id: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
