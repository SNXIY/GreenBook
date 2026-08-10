"""API-layer adapter from a conversation turn into Runtime execution.

This module is deliberately not wired into the production message route yet.
It provides a narrow, injectable seam for the next migration step:

    message -> IntentSpecProvider -> TaskProvider -> IntentCompiler
             -> RuntimeAgentService

The adapter owns request-level binding only.  Runtime execution state remains
owned by RuntimeAgentService and its existing PlanExecution repositories.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.task.intent_compat import to_task_intent
from greenbook_assistant_core.task.intent_models import IntentSpec
from greenbook_assistant_core.task.intent_spec_provider import (
    IntentSpecProvider,
    IntentSpecProviderError,
)
from greenbook_assistant_core.task.models import Task

from ..models.runtime_context import RuntimeContext
from ..models.runtime_result import RuntimeResult
from .intent_compiler import IntentCompilationError, IntentCompiler
from .runtime_agent_service import RuntimeAgentService
from .task_provider import TaskBinding, TaskProvider, TaskProviderError, TaskScope


class ConversationRuntimeAdapter:
    """Resolve one conversation turn and hand it to the Runtime service.

    Dependencies are injectable so this boundary can be tested without an LLM,
    database, MCP server, or production route registration.
    """

    def __init__(
        self,
        *,
        intent_provider: IntentSpecProvider | Any | None = None,
        task_provider: TaskProvider | Any | None = None,
        intent_compiler: IntentCompiler | None = None,
        runtime_service: RuntimeAgentService | Any | None = None,
        execution_repository: Any | None = None,
    ) -> None:
        self._intent_provider = intent_provider or IntentSpecProvider()
        self._task_provider = task_provider or TaskProvider()
        self._intent_compiler = intent_compiler or IntentCompiler()
        self._runtime_service = runtime_service or RuntimeAgentService(
            repository=execution_repository,
        )
        self._execution_repository = execution_repository

    async def execute(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        message: str,
        history: Sequence[Mapping[str, str]] | None = None,
        session: SessionContext | Any | None = None,
        timezone: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        mcp: Any = None,
        llm: Any = None,
        model: str = "",
        auth: Any = None,
        detach: bool = False,
        completion_callback: Any = None,
        existing_tasks: Sequence[Mapping[str, str]] | None = None,
    ) -> RuntimeResult:
        """Adapt one old message request into a RuntimeResult.

        The method intentionally does not read or write ``assistant_runs``.
        Any provider/compiler failure is represented as a failed RuntimeResult
        so callers get one stable envelope instead of a legacy success message.
        """

        request_session = self._coerce_session(
            session,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone or "Asia/Shanghai",
        )
        request_timezone = timezone or getattr(
            request_session, "timezone", "Asia/Shanghai"
        )
        scope = TaskScope(
            user_id=user_id,
            tenant_id=tenant_id,
            conversation_id=conversation_id,
        )

        intent_spec: IntentSpec | None = None
        task: Task | None = None
        try:
            self._validate_session_scope(
                request_session,
                user_id=user_id,
                tenant_id=tenant_id,
                conversation_id=conversation_id,
            )
            # Intent understanding is the first semantic boundary.  If the
            # caller already has task hints, pass a detached copy; otherwise
            # TaskProvider performs the scoped lookup after IntentSpec exists.
            task_hints = (
                [dict(item) for item in existing_tasks]
                if existing_tasks is not None else None
            )
            intent_spec = await self._intent_provider.resolve(
                message,
                existing_tasks=task_hints,
            )
            task_intent = to_task_intent(intent_spec)

            binding: TaskBinding | None = None
            relation = str(task_intent.relation)
            if relation in {"NEW_TASK", "DIRECT", "QUERY_TASK"}:
                task = await self._task_provider.create_task(scope, intent_spec)
            else:
                binding = await self._task_provider.resolve_task(scope, task_intent)
                task = binding.task

            target_context = binding.target if binding is not None else None
            task_context = self._intent_compiler.compile(
                intent_spec=intent_spec,
                target_context=target_context,
                task=task,
                conversation=request_session,
                artifacts=task.artifacts,
                timezone=request_timezone,
            )

            # Keep the current in-memory SessionContext useful for the next
            # turn.  Persistence remains the caller's responsibility; this is
            # not a database write or an authorization shortcut.
            self._set_session_binding(
                request_session,
                task_id=task.task_id,
                artifact_id=task_context.active_artifact_id,
            )

            context = RuntimeContext(
                conversation_id=conversation_id,
                run_id=run_id or str(uuid.uuid4()),
                trace_id=trace_id or str(uuid.uuid4()),
                task_id=task.task_id,
                task_context=task_context,
                user_id=user_id,
                tenant_id=tenant_id,
                timezone=request_timezone,
                user_message=message,
                conversation_history=[dict(item) for item in (history or ())],
                task_intent=task_context.task_intent,
                session=request_session,
                active_artifact_id=task_context.active_artifact_id,
                active_draft_id=getattr(request_session, "active_draft_id", None),
                active_schedule_id=getattr(request_session, "active_schedule_id", None),
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
            )

            result = await self._runtime_service.execute(
                context,
                detach=detach,
                completion_callback=completion_callback,
            )
            return await self._complete_result(
                result,
                intent_spec=intent_spec,
                task_id=task.task_id,
            )
        except (
            IntentSpecProviderError,
            TaskProviderError,
            IntentCompilationError,
        ) as exc:
            return self._failure_result(
                exc,
                intent_spec=intent_spec,
                task_id=task.task_id if task is not None else "",
                run_id=run_id or "",
                trace_id=trace_id or "",
            )
        except Exception as exc:
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=run_id or "",
                task_id=task.task_id if task is not None else "",
                execution_path="runtime",
                error_code="RUNTIME_ADAPTER_FAILED",
                error_message=str(exc) or "Runtime adapter failed",
                intent_spec=(
                    intent_spec.model_dump(mode="json")
                    if intent_spec is not None else None
                ),
                trace_id=trace_id or "",
            )

    async def run(self, **kwargs: Any) -> RuntimeResult:
        """Convenience alias for callers that name the operation ``run``."""

        return await self.execute(**kwargs)

    async def _complete_result(
        self,
        result: RuntimeResult,
        *,
        intent_spec: IntentSpec,
        task_id: str,
    ) -> RuntimeResult:
        result.intent_spec = intent_spec.model_dump(mode="json")
        if not result.task_id:
            result.task_id = task_id
        if not result.plan_id and result.execution_id:
            execution = await self._find_execution(result.execution_id)
            if execution is not None:
                result.plan_id = execution.plan_id
        return result

    async def _find_execution(self, execution_id: str) -> Any | None:
        repository = self._execution_repository
        if repository is None:
            repository = getattr(self._runtime_service, "_execution_repository", None)
        if repository is None:
            repository = ExecutionRepository()
        finder = getattr(repository, "find_by_id", None)
        if finder is None:
            return None
        try:
            execution = finder(execution_id)
        except Exception:
            # Plan metadata is an enrichment of RuntimeResult.  A repository
            # read failure must not rewrite a completed/failed Runtime result.
            return None
        if inspect.isawaitable(execution):
            try:
                execution = await execution
            except Exception:
                return None
        return execution

    @staticmethod
    def _failure_result(
        exc: Exception,
        *,
        intent_spec: IntentSpec | None,
        task_id: str,
        run_id: str,
        trace_id: str,
    ) -> RuntimeResult:
        code = str(getattr(exc, "code", "RUNTIME_ADAPTER_FAILED"))
        return RuntimeResult(
            success=False,
            status="FAILED",
            run_id=run_id,
            task_id=task_id,
            execution_path="runtime",
            error_code=code,
            error_message=str(exc),
            intent_spec=(
                intent_spec.model_dump(mode="json")
                if intent_spec is not None else None
            ),
            trace_id=trace_id,
        )

    @staticmethod
    def _coerce_session(
        session: SessionContext | Any | None,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        timezone: str,
    ) -> SessionContext | Any:
        if session is not None:
            return session
        return SessionContext(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            timezone=timezone,
        )

    @staticmethod
    def _validate_session_scope(
        session: Any,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
    ) -> None:
        for field, expected in (
            ("conversation_id", conversation_id),
            ("user_id", user_id),
            ("tenant_id", tenant_id),
        ):
            actual = getattr(session, field, expected)
            if actual != expected:
                raise TaskProviderError(
                    "SESSION_SCOPE_MISMATCH",
                    f"Session {field} does not match the authenticated request.",
                )

    @staticmethod
    def _set_session_binding(
        session: Any,
        *,
        task_id: str,
        artifact_id: str | None,
    ) -> None:
        if hasattr(session, "active_task_id"):
            session.active_task_id = task_id
        if hasattr(session, "active_artifact_id"):
            session.active_artifact_id = artifact_id


__all__ = ["ConversationRuntimeAdapter"]
