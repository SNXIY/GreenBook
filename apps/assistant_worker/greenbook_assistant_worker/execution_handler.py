"""Concrete Runtime handler used by the standalone Execution Queue worker."""

from __future__ import annotations

from typing import Any

from greenbook_assistant_api.services.runtime_agent_service import RuntimeAgentService
from greenbook_assistant_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_assistant_core.execution.operation_tracking import ExternalOperationTracker
from greenbook_assistant_core.execution.retry_scheduler import RetryScheduler
from greenbook_assistant_core.observability.metrics import MetricsCollector
from greenbook_contracts.identity import AuthContext
from greenbook_mcp_server.server import GreenBookMCPServer


class RuntimeExecutionQueueHandler:
    """Rebuild process-local Runtime adapters for one queued Execution.

    The queue payload contains identity metadata but never a bearer token. A
    deployment supplies a worker/service access token through the process
    secret configuration; it is used only in memory while invoking Java.
    """

    def __init__(
        self,
        *,
        repository: Any,
        event_store: Any,
        checkpoint_store: Any,
        external_operation_store: Any,
        mcp: GreenBookMCPServer,
        worker_access_token: str,
        llm: Any = None,
        model: str = "",
        metrics_collector: MetricsCollector | None = None,
        retry_scheduler: RetryScheduler | None = None,
    ) -> None:
        if not worker_access_token:
            raise RuntimeError(
                "ASSISTANT_WORKER_ACCESS_TOKEN is required for queued Runtime execution"
            )
        self._mcp = mcp
        self._worker_access_token = worker_access_token
        self._llm = llm
        self._model = model
        self._service = RuntimeAgentService(
            repository=repository,
            event_store=event_store,
            checkpoint_store=checkpoint_store,
            operation_tracker=ExternalOperationTracker(
                store=external_operation_store,
            ),
            metrics_collector=metrics_collector,
            retry_scheduler=retry_scheduler,
        )

    async def __call__(self, message: ExecutionQueueMessage) -> None:
        identity = message.payload.get("auth_context") or {}
        user_id = str(identity.get("user_id") or message.payload.get("user_id") or "")
        tenant_id = str(
            identity.get("tenant_id") or message.payload.get("tenant_id") or ""
        )
        if not user_id or not tenant_id:
            raise RuntimeError(
                f"Queued execution {message.execution_id} has no authenticated scope"
            )

        auth = AuthContext(
            user_id=user_id,
            tenant_id=tenant_id,
            roles=[str(role) for role in (identity.get("roles") or [])],
            session_id=identity.get("session_id"),
            token_id=identity.get("token_id"),
            timezone=str(
                identity.get("timezone")
                or message.payload.get("timezone")
                or "Asia/Shanghai"
            ),
            raw_access_token=self._worker_access_token,
        )
        result = await self._service.execute_queued(
            message,
            mcp=self._mcp,
            llm=self._llm,
            model=self._model,
            auth=auth,
        )
        if result.error_code in {
            "EXECUTION_DISPATCH_INVALID",
            "EXECUTION_NOT_FOUND",
        }:
            raise RuntimeError(
                f"Queued execution {message.execution_id} could not be dispatched: "
                f"{result.error_code}"
            )


__all__ = ["RuntimeExecutionQueueHandler"]
