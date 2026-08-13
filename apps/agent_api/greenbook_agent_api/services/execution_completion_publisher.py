"""Publish durable Agent projections after queued Runtime completion."""

from __future__ import annotations

from typing import Any

from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.result_projection import (
    MemoryExecutionResultProjectionStore,
)
from greenbook_contracts.identity import AuthContext

from .completion_projection_coordinator import CompletionProjectionCoordinator
from .result_resolver import ResultResolver


class ExecutionCompletionPublisher:
    """Compatibility callback delegating to the Phase17-C coordinator."""

    def __init__(
        self,
        *,
        conversation_service: Any | None = None,
        run_store: dict[str, Any] | None = None,
        artifact_store: Any | None = None,
        result_projection_store: Any | None = None,
        task_provider: Any | None = None,
        coordinator: CompletionProjectionCoordinator | None = None,
        approval_service: Any | None = None,
    ) -> None:
        self._approval_service = approval_service
        if coordinator is not None:
            self._coordinator = coordinator
            return
        if conversation_service is None:
            raise ValueError("ExecutionCompletionPublisher requires a context manager")
        self._coordinator = CompletionProjectionCoordinator(
            conversation_service=conversation_service,
            result_projection_store=(
                result_projection_store or MemoryExecutionResultProjectionStore()
            ),
            result_resolver=ResultResolver(artifact_store=artifact_store),
            task_provider=task_provider,
            run_store=run_store,
        )

    async def __call__(
        self,
        message: ExecutionQueueMessage,
        result: Any,
        auth: AuthContext,
    ) -> None:
        if self._approval_service is not None:
            identity = message.payload.get("auth_context") or {}
            await self._approval_service.capture_result(
                result,
                conversation_id=str(message.payload.get("conversation_id") or ""),
                user_id=str(identity.get("user_id") or message.payload.get("user_id") or ""),
                tenant_id=str(
                    identity.get("tenant_id")
                    or message.payload.get("tenant_id")
                    or ""
                ),
            )
        await self._coordinator.complete(message, result, auth)

    async def reconcile(self, message: ExecutionQueueMessage, execution: Any) -> bool:
        if self._approval_service is not None:
            await self._approval_service.reconcile_execution(message, execution)
        return await self._coordinator.reconcile(message, execution)


__all__ = ["ExecutionCompletionPublisher"]
