"""Durable approval request model and storage ports."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from greenbook_agent_core.db.connection import session_ctx
from greenbook_agent_core.db.repositories import ApprovalRepository


class ApprovalRequestStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str
    conversation_id: str
    user_id: str
    tenant_id: str
    message: str
    operation: str = "RUNTIME_APPROVAL"
    resource_id: str | None = None
    run_id: str | None = None
    status: ApprovalRequestStatus = ApprovalRequestStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ApprovalRequestStore(Protocol):
    async def save(self, request: ApprovalRequest) -> ApprovalRequest: ...
    async def find_by_id(self, approval_id: str) -> ApprovalRequest | None: ...
    async def find_by_execution(self, execution_id: str) -> ApprovalRequest | None: ...
    async def update_status(
        self,
        approval_id: str,
        status: ApprovalRequestStatus,
    ) -> ApprovalRequest | None: ...
    async def transition(
        self,
        approval_id: str,
        status: ApprovalRequestStatus,
    ) -> ApprovalRequest | None: ...


class ApprovalTransitionConflictError(RuntimeError):
    """Raised when a concurrent decision already transitioned the request."""


class MemoryApprovalRequestStore:
    def __init__(self) -> None:
        self._items: dict[str, ApprovalRequest] = {}

    async def save(self, request: ApprovalRequest) -> ApprovalRequest:
        self._items[request.approval_id] = request.model_copy(deep=True)
        return request

    async def find_by_id(self, approval_id: str) -> ApprovalRequest | None:
        item = self._items.get(approval_id)
        return item.model_copy(deep=True) if item else None

    async def find_by_execution(self, execution_id: str) -> ApprovalRequest | None:
        return next(
            (
                item.model_copy(deep=True)
                for item in reversed(list(self._items.values()))
                if item.execution_id == execution_id
            ),
            None,
        )

    async def update_status(
        self,
        approval_id: str,
        status: ApprovalRequestStatus,
    ) -> ApprovalRequest | None:
        item = self._items.get(approval_id)
        if item is None:
            return None
        item.status = status
        return item.model_copy(deep=True)

    async def transition(
        self,
        approval_id: str,
        status: ApprovalRequestStatus,
    ) -> ApprovalRequest | None:
        """Atomic PENDING -> ``status`` CAS (single-process memory store)."""
        item = self._items.get(approval_id)
        if item is None:
            return None
        if item.status != ApprovalRequestStatus.PENDING:
            raise ApprovalTransitionConflictError(approval_id)
        item.status = status
        return item.model_copy(deep=True)


class PostgresApprovalRequestStore:
    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory or session_ctx

    async def save(self, request: ApprovalRequest) -> ApprovalRequest:
        async with self._session_factory() as session:
            await ApprovalRepository(session).create(
                approval_id=request.approval_id,
                conversation_id=request.conversation_id,
                run_id=request.run_id,
                execution_id=request.execution_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                operation=request.operation,
                resource_id=request.resource_id,
                description=request.message,
                payload=request.payload,
                status=request.status.value,
                created_at=datetime.fromisoformat(request.created_at),
            )
        return request

    async def find_by_id(self, approval_id: str) -> ApprovalRequest | None:
        async with self._session_factory() as session:
            row = await ApprovalRepository(session).find_by_id(approval_id)
        return _from_row(row)

    async def find_by_execution(self, execution_id: str) -> ApprovalRequest | None:
        async with self._session_factory() as session:
            row = await ApprovalRepository(session).find_by_execution_id(execution_id)
        return _from_row(row)

    async def update_status(
        self,
        approval_id: str,
        status: ApprovalRequestStatus,
    ) -> ApprovalRequest | None:
        async with self._session_factory() as session:
            row = await ApprovalRepository(session).update(
                approval_id,
                status=status.value,
            )
        return _from_row(row)

    async def transition(
        self,
        approval_id: str,
        status: ApprovalRequestStatus,
    ) -> ApprovalRequest | None:
        """Atomic PENDING -> ``status`` flip backed by a WHERE status='PENDING'
        update with rowcount enforcement; a concurrent decision raises."""
        from greenbook_agent_core.db.repositories import _ApprovalVersionConflictError

        async with self._session_factory() as session:
            try:
                row = await ApprovalRepository(session).transition(
                    approval_id,
                    status=status.value,
                )
            except _ApprovalVersionConflictError as exc:
                raise ApprovalTransitionConflictError(approval_id) from exc
        return _from_row(row)


def _from_row(row: dict[str, Any] | None) -> ApprovalRequest | None:
    if row is None:
        return None
    created = row.get("created_at")
    return ApprovalRequest(
        approval_id=str(row.get("approval_id") or ""),
        execution_id=str(row.get("execution_id") or ""),
        conversation_id=str(row.get("conversation_id") or ""),
        user_id=str(row.get("user_id") or ""),
        tenant_id=str(row.get("tenant_id") or ""),
        message=str(row.get("description") or ""),
        operation=str(row.get("operation") or "RUNTIME_APPROVAL"),
        resource_id=row.get("resource_id"),
        run_id=str(row.get("run_id")) if row.get("run_id") else None,
        status=ApprovalRequestStatus(str(row.get("status") or "PENDING")),
        payload=dict(row.get("payload") or {}),
        created_at=(
            created.isoformat() if hasattr(created, "isoformat") else str(created)
        ),
    )


__all__ = [
    "ApprovalRequest",
    "ApprovalRequestStatus",
    "ApprovalRequestStore",
    "ApprovalTransitionConflictError",
    "MemoryApprovalRequestStore",
    "PostgresApprovalRequestStore",
]

