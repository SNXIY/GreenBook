from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

CallbackDeliveryStatus = Literal[
    "PENDING",
    "DELIVERING",
    "RETRYING",
    "DELIVERED",
    "DEAD",
]


class ModerationCallbackDeliveryView(BaseModel):
    id: UUID
    task_id: UUID
    task_version: int
    status: CallbackDeliveryStatus
    attempts: int
    max_attempts: int
    available_at: datetime
    last_http_status: int | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None = None
