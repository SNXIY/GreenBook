from __future__ import annotations

import asyncio
from datetime import datetime

from app.creator.tools.errors import CreatorToolAuditError
from app.creator.tools.models import (
    CreatorToolCallAudit,
    CreatorToolCallStatus,
)


class InMemoryCreatorToolAuditStore:
    backend_name = "in-memory"

    def __init__(self) -> None:
        self._records: dict[str, CreatorToolCallAudit] = {}
        self._lock = asyncio.Lock()

    async def start(self, audit: CreatorToolCallAudit) -> None:
        async with self._lock:
            if audit.call_id in self._records:
                raise CreatorToolAuditError(
                    f"Tool audit {audit.call_id} already exists",
                    call_id=audit.call_id,
                )
            self._records[audit.call_id] = audit

    async def finish(
        self,
        *,
        call_id: str,
        status: CreatorToolCallStatus,
        finished_at: datetime,
        latency_ms: int,
        result_sha256: str | None,
        result_size_bytes: int | None,
        error_code: str | None,
    ) -> None:
        async with self._lock:
            current = self._records.get(call_id)
            if current is None or current.status != CreatorToolCallStatus.RUNNING:
                raise CreatorToolAuditError(
                    f"Tool audit {call_id} cannot be finalized",
                    call_id=call_id,
                )
            self._records[call_id] = current.model_copy(
                update={
                    "status": status,
                    "finished_at": finished_at,
                    "latency_ms": latency_ms,
                    "result_sha256": result_sha256,
                    "result_size_bytes": result_size_bytes,
                    "error_code": error_code,
                }
            )

    async def get(self, call_id: str) -> CreatorToolCallAudit | None:
        async with self._lock:
            return self._records.get(call_id)

    @property
    def records(self) -> tuple[CreatorToolCallAudit, ...]:
        return tuple(self._records.values())
