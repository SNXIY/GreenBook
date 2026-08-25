"""Publish durable Agent projections after queued Runtime completion."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from inspect import isawaitable
from typing import Any

from greenbook_contracts.identity import AuthContext

from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.result_projection import (
    MemoryExecutionResultProjectionStore,
)

from .completion_projection import CompletionProjectionCoordinator
from .result_resolver import ResultResolver

logger = logging.getLogger(__name__)


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
        user_activity_publisher: Any | None = None,
        after_execution: Callable[[ExecutionQueueMessage, Any], Awaitable[None] | None]
        | None = None,
    ) -> None:
        self._approval_service = approval_service
        self._user_activity_publisher = user_activity_publisher
        self._after_execution = after_execution
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
            approval = await self._approval_service.capture_result(
                result,
                conversation_id=str(message.payload.get("conversation_id") or ""),
                user_id=str(identity.get("user_id") or message.payload.get("user_id") or ""),
                tenant_id=str(
                    identity.get("tenant_id")
                    or message.payload.get("tenant_id")
                    or ""
                ),
            )
            # Preserve the durable approval identity for the same completion
            # projection that updates the Agent Run.  The approval store is
            # authoritative; this only carries its ID across the boundary.
            if approval is not None:
                result.approval_id = str(approval.approval_id)
        await self._coordinator.complete(message, result, auth)
        if self._user_activity_publisher is not None:
            try:
                self._user_activity_publisher.publish_runtime_result(
                    result,
                    conversation_id=str(message.payload.get("conversation_id") or ""),
                    user_id=auth.user_id,
                    tenant_id=auth.tenant_id,
                    run_id=str(result.run_id or message.payload.get("run_id") or "") or None,
                )
            except Exception:
                # Projection storage is not allowed to make a successfully
                # completed, idempotent business operation retry its tool.
                logger.exception(
                    "User activity completion projection failed execution_id=%s",
                    message.execution_id,
                )

    async def after_execution(
        self,
        message: ExecutionQueueMessage,
        result: Any,
    ) -> None:
        """Run post-observation bookkeeping after the queue handler writes evidence.

        Completion projection must happen before ``ActionObservation`` is
        persisted.  Run convergence happens after that marker exists, so a
        continuation cannot be mistaken for a completed Run while its
        observation is still waiting to be consumed.
        """

        if self._after_execution is None:
            return
        callback_result = self._after_execution(message, result)
        if isawaitable(callback_result):
            await callback_result

    async def reconcile(
        self,
        message: ExecutionQueueMessage,
        execution: Any,
        *,
        result: Any | None = None,
    ) -> bool:
        if self._approval_service is not None:
            await self._approval_service.reconcile_execution(message, execution)
        return await self._coordinator.reconcile(
            message,
            execution,
            result=result,
        )


__all__ = ["ExecutionCompletionPublisher"]
