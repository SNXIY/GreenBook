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
from greenbook_assistant_core.task.multi_task import (
    ConversationTaskIndex,
    ConversationTargetResolver,
    TaskSegment,
    intent_delta_from_spec,
    split_task_segments,
)
from greenbook_assistant_core.task.models import (
    ArtifactRef,
    TaskExecutionRef,
    TaskGoal,
    TaskResourceRef,
)

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
        self._task_indexes: dict[str, ConversationTaskIndex] = {}

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
        _skip_multi: bool = False,
        _intent_spec_override: IntentSpec | None = None,
        _resolved_target_id: str | None = None,
    ) -> RuntimeResult:
        """Adapt one old message request into a RuntimeResult.

        The method intentionally does not read or write ``assistant_runs``.
        Any provider/compiler failure is represented as a failed RuntimeResult
        so callers get one stable envelope instead of a legacy success message.
        """

        if not _skip_multi:
            segments = split_task_segments(message)
            if len(segments) > 1:
                return await self.execute_many(
                    conversation_id=conversation_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                    message=message,
                    segments=segments,
                    history=history,
                    session=session,
                    timezone=timezone,
                    run_id=run_id,
                    trace_id=trace_id,
                    mcp=mcp,
                    llm=llm,
                    model=model,
                    auth=auth,
                    detach=detach,
                    completion_callback=completion_callback,
                    existing_tasks=existing_tasks,
                )

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
            intent_spec = _intent_spec_override or await self._intent_provider.resolve(
                message,
                existing_tasks=task_hints,
            )
            task_intent = to_task_intent(intent_spec)
            # Resolve structured conversation references before the legacy
            # TaskProvider fallback.  This covers a single-message weak
            # reference as well as multi-task child segments.
            if str(task_intent.relation) in {
                "CONTINUE_TASK", "MODIFY_TASK", "CANCEL_TASK",
            } and not task_intent.target_task_id:
                task_index = self._task_indexes.setdefault(
                    conversation_id, ConversationTaskIndex(),
                )
                try:
                    known_tasks = await self._task_provider.list_tasks(scope)
                except Exception:
                    known_tasks = []
                for known_task in known_tasks:
                    task_index.register(known_task)
                resolution = task_index.resolve(message)
                if resolution.is_ambiguous:
                    raise TaskProviderError(
                        "TASK_TARGET_AMBIGUOUS",
                        "Multiple Tasks match the requested target.",
                        candidates=[task.task_id for task in resolution.candidates],
                    )
                if resolution.task is not None:
                    task_intent.target_task_id = resolution.task.task_id
            if _resolved_target_id:
                task_intent.target_task_id = _resolved_target_id

            binding: TaskBinding | None = None
            relation = str(task_intent.relation)
            # QUERY is a read-only conversation result.  It must not create a
            # Task or enqueue an Execution merely because it arrived via the
            # Runtime message endpoint.
            if relation == "DIRECT" and all(
                str(action.action) == "QUERY" for action in intent_spec.actions
            ):
                return self._query_result(
                    intent_spec,
                    run_id=run_id or "",
                    trace_id=trace_id or "",
                )
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
            completed = await self._complete_result(
                result,
                intent_spec=intent_spec,
                task_id=task.task_id,
            )
            self._sync_task_index(task, intent_spec, completed)
            await self._persist_task_projection(scope, task)
            return completed
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

    async def get_task_index(
        self, *, conversation_id: str, user_id: str, tenant_id: str,
    ) -> list[dict[str, object]]:
        """Return the scoped conversation Task/Goal/Execution projection."""
        scope = TaskScope(
            user_id=user_id, tenant_id=tenant_id, conversation_id=conversation_id,
        )
        index = self._task_indexes.setdefault(conversation_id, ConversationTaskIndex())
        for task in await self._task_provider.list_tasks(scope):
            index.register(task)
        return index.snapshot()

    async def execute_many(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        message: str,
        segments: Sequence[TaskSegment] | None = None,
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
        """Resolve and dispatch each independent Task through normal Runtime.

        Each child keeps its own Task and Execution.  The aggregate is only a
        presentation envelope; Queue/Worker still see ordinary executions.
        """
        child_segments = list(segments or split_task_segments(message))
        scope = TaskScope(user_id=user_id, tenant_id=tenant_id, conversation_id=conversation_id)
        index = self._task_indexes.setdefault(conversation_id, ConversationTaskIndex())
        try:
            tasks = await self._task_provider.list_tasks(scope)
        except Exception:
            tasks = []
        for task in tasks:
            index.register(task)

        results: list[RuntimeResult] = []
        for child in child_segments:
            child_spec = await self._intent_provider.resolve(
                child.text,
                existing_tasks=existing_tasks,
            )
            if child.is_query:
                results.append(self._query_result(
                    child_spec,
                    run_id=run_id or "",
                    trace_id=trace_id or "",
                ))
                continue

            target_id: str | None = None
            relation = str(to_task_intent(child_spec).relation)
            if relation in {"CONTINUE_TASK", "MODIFY_TASK", "CANCEL_TASK"}:
                resolution = ConversationTargetResolver().resolve(child.text, index.list())
                if resolution.is_ambiguous:
                    results.append(RuntimeResult(
                        success=False, status="FAILED", run_id=run_id or "",
                        trace_id=trace_id or "", execution_path="runtime",
                        error_code="TASK_TARGET_AMBIGUOUS",
                        error_message="Multiple Tasks match the requested target.",
                        partial_results={"candidates": [task.task_id for task in resolution.candidates]},
                    ))
                    continue
                if resolution.task is not None:
                    target_id = resolution.task.task_id
                elif not child_spec.target_hint:
                    results.append(RuntimeResult(
                        success=False, status="FAILED", run_id=run_id or "",
                        trace_id=trace_id or "", execution_path="runtime",
                        error_code="TASK_NOT_FOUND",
                        error_message="No Task matches the requested target.",
                    ))
                    continue

            child_result = await self.execute(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                message=child.text,
                history=history,
                session=session,
                timezone=timezone,
                run_id=run_id,
                trace_id=trace_id,
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
                detach=detach,
                completion_callback=completion_callback,
                existing_tasks=existing_tasks,
                _skip_multi=True,
                _intent_spec_override=child_spec,
                _resolved_target_id=target_id,
            )
            results.append(child_result)

        if not results:
            return RuntimeResult(success=True, status="COMPLETED", run_id=run_id or "", trace_id=trace_id or "")
        successes = [result for result in results if result.success]
        execution_ids = [result.execution_id for result in results if result.execution_id]
        task_ids = [result.task_id for result in results if result.task_id]
        status = "COMPLETED" if len(successes) == len(results) else "PARTIAL"
        return RuntimeResult(
            success=len(successes) == len(results),
            status=status,
            run_id=run_id or next((result.run_id for result in results if result.run_id), ""),
            trace_id=trace_id or next((result.trace_id for result in results if result.trace_id), ""),
            task_id=task_ids[0] if task_ids else "",
            execution_id=execution_ids[0] if execution_ids else None,
            execution_path="runtime",
            content="\n\n".join(result.content for result in results if result.content),
            error_code=next((result.error_code for result in results if result.error_code), ""),
            partial_results={
                "multi_task": True,
                "task_ids": task_ids,
                "execution_ids": execution_ids,
                "results": [
                    {
                        "task_id": result.task_id,
                        "execution_id": result.execution_id,
                        "status": result.status,
                        "success": result.success,
                        "error_code": result.error_code,
                    }
                    for result in results
                ],
            },
        )

    @staticmethod
    def _query_result(
        intent_spec: IntentSpec, *, run_id: str, trace_id: str,
    ) -> RuntimeResult:
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=run_id,
            trace_id=trace_id,
            execution_path="runtime",
            content=intent_spec.goal or "查询已完成。",
            summary="QUERY completed without creating a Task or Execution",
            intent_spec=intent_spec.model_dump(mode="json"),
            partial_results={"query_only": True, "side_effect": False},
        )

    def _sync_task_index(self, task: Task, spec: IntentSpec, result: RuntimeResult) -> None:
        index = self._task_indexes.setdefault(task.conversation_id, ConversationTaskIndex())
        index.register(task)
        if not task.goals:
            for action in spec.actions or []:
                goal = TaskGoal(
                    task_id=task.task_id,
                    description=spec.goal,
                    kind=str(action.action),
                )
                index.record_goal(task.task_id, goal)
        if result.execution_id:
            index.record_execution(task.task_id, TaskExecutionRef(
                execution_id=result.execution_id,
                task_id=task.task_id,
                status=result.status,
            ))
        if result.draft_id:
            index.record_resource(task.task_id, TaskResourceRef(
                resource_id=result.draft_id, resource_kind="DRAFT",
            ))
        if result.schedule_id:
            index.record_resource(task.task_id, TaskResourceRef(
                resource_id=result.schedule_id, resource_kind="SCHEDULE",
            ))
        delta = intent_delta_from_spec(spec, spec.goal, [task.task_id])
        for operation in delta.operations:
            index.mark_action(task.task_id, operation)

    async def _persist_task_projection(self, scope: TaskScope, task: Task) -> None:
        persist = getattr(self._task_provider, "persist_projection", None)
        if persist is None:
            return
        try:
            maybe = persist(scope, task)
            if inspect.isawaitable(maybe):
                await maybe
        except Exception:
            # Runtime completion is canonical.  A projection write must not
            # turn a successful execution into a business failure; the next
            # request can rebuild the index from the Task store.
            return

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
