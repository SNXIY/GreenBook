"""Runtime dispatch adapter shared by API-managed and standalone workers."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.operation_tracking import ExternalOperationTracker
from greenbook_agent_core.execution.retry_scheduler import RetryScheduler
from greenbook_agent_core.observability.metrics import MetricsCollector
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_contracts.identity import AuthContext
from greenbook_mcp_server.server import GreenBookMCPServer

from .runtime_agent_service import RuntimeAgentService

CredentialResolver = Callable[[ExecutionQueueMessage], AuthContext]
CompletionPublisher = Callable[
    [ExecutionQueueMessage, Any, AuthContext],
    Awaitable[None] | None,
]


class RuntimeExecutionQueueHandler:
    """Execute one durable queue message through ``RuntimeAgentService``.

    A standalone deployment may supply a service token.  The local combined
    API process instead supplies a credential resolver backed only by tokens
    previously validated by the API middleware.
    """

    def __init__(
        self,
        *,
        mcp: GreenBookMCPServer,
        service: RuntimeAgentService | None = None,
        repository: Any = None,
        event_store: Any = None,
        checkpoint_store: Any = None,
        external_operation_store: Any = None,
        worker_access_token: str = "",
        credential_resolver: CredentialResolver | None = None,
        completion_publisher: CompletionPublisher | None = None,
        llm: Any = None,
        model: str = "",
        metrics_collector: MetricsCollector | None = None,
        retry_scheduler: RetryScheduler | None = None,
        container: RuntimeContainer | None = None,
        memory_manager: Any | None = None,
    ) -> None:
        if credential_resolver is None and not worker_access_token:
            raise RuntimeError(
                "Queued Runtime execution requires a credential resolver or "
                "GREENBOOK_AGENT_WORKER_ACCESS_TOKEN"
            )
        self._mcp = mcp
        self._worker_access_token = worker_access_token
        self._credential_resolver = credential_resolver
        self._completion_publisher = completion_publisher
        self._llm = llm
        self._model = model
        self._service = service or RuntimeAgentService(
            container=container,
            repository=repository,
            event_store=event_store,
            checkpoint_store=checkpoint_store,
            artifact_store=(container.artifact_store if container is not None else None),
            operation_tracker=ExternalOperationTracker(
                store=external_operation_store,
            ),
            metrics_collector=metrics_collector,
            retry_scheduler=retry_scheduler,
            memory_manager=memory_manager,
        )

    async def __call__(self, message: ExecutionQueueMessage) -> None:
        auth = (
            self._credential_resolver(message)
            if self._credential_resolver is not None
            else self._service_auth(message)
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
        if self._completion_publisher is not None:
            published = self._completion_publisher(message, result, auth)
            if inspect.isawaitable(published):
                await published

    def _service_auth(self, message: ExecutionQueueMessage) -> AuthContext:
        identity = message.payload.get("auth_context") or {}
        user_id = str(identity.get("user_id") or message.payload.get("user_id") or "")
        tenant_id = str(
            identity.get("tenant_id") or message.payload.get("tenant_id") or ""
        )
        if not user_id or not tenant_id:
            raise RuntimeError(
                f"Queued execution {message.execution_id} has no authenticated scope"
            )
        return AuthContext(
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


__all__ = [
    "CompletionPublisher",
    "CredentialResolver",
    "RuntimeExecutionQueueHandler",
]
