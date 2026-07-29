from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel

from app.creator.tools.models import (
    CreatorToolCallAudit,
    CreatorToolCallContext,
    CreatorToolCallStatus,
    CreatorToolResult,
    ToolHandlerResult,
)


class CreatorToolHandler(Protocol):
    async def __call__(
        self,
        request: BaseModel,
        context: CreatorToolCallContext,
    ) -> ToolHandlerResult: ...


class CreatorToolAuditStore(Protocol):
    backend_name: str

    async def start(self, audit: CreatorToolCallAudit) -> None: ...

    async def finish(
        self,
        *,
        call_id: str,
        status: CreatorToolCallStatus,
        finished_at,
        latency_ms: int,
        result_sha256: str | None,
        result_size_bytes: int | None,
        error_code: str | None,
    ) -> None: ...

    async def get(self, call_id: str) -> CreatorToolCallAudit | None: ...


class CreatorToolInvoker(Protocol):
    async def call(
        self,
        name: str,
        arguments: dict,
        context: CreatorToolCallContext,
    ) -> CreatorToolResult: ...
