"""Single approval decision boundary for legacy and Runtime API routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..models.runtime_result import RuntimeResult


class ApprovalDecisionService:
    """Coordinate one approval record with optional Runtime resumption."""

    def __init__(
        self,
        *,
        update_status: Callable[..., Awaitable[dict[str, Any] | None]],
        resume_runtime: Callable[[str, str], Awaitable[RuntimeResult | None]],
    ) -> None:
        self._update_status = update_status
        self._resume_runtime = resume_runtime

    async def decide(
        self,
        record: dict[str, Any],
        *,
        decision: str,
    ) -> dict[str, Any]:
        approval_id = str(record.get("approval_id", ""))
        execution_id = record.get("execution_id")
        runtime_result: RuntimeResult | None = None

        if decision == "APPROVE" and execution_id:
            runtime_result = await self._resume_runtime(approval_id, "ACCEPT")
        elif decision == "REJECT" and execution_id:
            runtime_result = await self._resume_runtime(approval_id, "REJECT")

        status = "APPROVED" if decision == "APPROVE" else "REJECTED"
        await self._update_status(approval_id, status=status)
        return {
            "approval_id": approval_id,
            "status": status,
            "execution_id": execution_id,
            "runtime_status": runtime_result.status if runtime_result else None,
        }


__all__ = ["ApprovalDecisionService"]
