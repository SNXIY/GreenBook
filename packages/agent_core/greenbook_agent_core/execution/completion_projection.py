"""Coordinate all durable read models after terminal Execution completion."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from greenbook_contracts.identity import AuthContext

from greenbook_agent_core.conversation import ConversationNotFoundError
from greenbook_agent_core.execution.execution_queue import ExecutionQueueMessage
from greenbook_agent_core.execution.result_projection import (
    ExecutionResultProjection,
    ExecutionResultProjectionStore,
)

from ..task.provider import TaskProvider, TaskScope
from .presenter import AgentResponse
from .projection_adapter import ExecutionProjectionAdapter
from .result_resolver import ResultResolver
from .runtime_result import RuntimeResult


class CompletionProjectionCoordinator:
    """Persist result, Task, Conversation context, and structured message."""

    def __init__(
        self,
        *,
        conversation_service: Any,
        result_projection_store: ExecutionResultProjectionStore,
        result_resolver: ResultResolver,
        task_provider: TaskProvider | None = None,
        projection_adapter: ExecutionProjectionAdapter | None = None,
        run_store: dict[str, Any] | None = None,
    ) -> None:
        self._conversation_service = conversation_service
        self._result_projection_store = result_projection_store
        self._result_resolver = result_resolver
        self._task_provider = task_provider
        self._projection_adapter = projection_adapter or ExecutionProjectionAdapter()
        self._run_store = run_store if run_store is not None else {}

    async def complete(
        self,
        message: ExecutionQueueMessage,
        result: RuntimeResult,
        auth: AuthContext,
        *,
        execution: Any | None = None,
    ) -> bool:
        payload = message.payload
        conversation_id = str(payload.get("conversation_id") or "")
        run_id = str(result.run_id or payload.get("run_id") or "")
        trace_id = str(result.trace_id or payload.get("trace_id") or message.trace_id)
        if not conversation_id or not run_id:
            raise RuntimeError(
                f"Queued execution {message.execution_id} has no conversation projection scope"
            )

        resolved = self._result_resolver.resolve(result, execution=execution)
        response = self._projection_adapter.project(resolved)
        result.content = response.message
        result.presentation = response.model_dump(mode="json")
        result.artifacts = list(resolved.artifacts)
        result.schedule = resolved.schedule
        result.draft_id = resolved.draft_id
        result.schedule_id = resolved.schedule_id

        # Task-level completion is derived from Goal satisfaction, never from
        # this single Execution's terminal state. The goal-aware projection
        # decides whether the Task (and its Goals) may be marked completed.
        task_status = str(resolved.status)
        task_id = str(resolved.task_id or payload.get("task_id") or "")
        if self._task_provider is not None and task_id:
            updated_task = await self._task_provider.persist_completion_projection(
                TaskScope(
                    user_id=auth.user_id,
                    tenant_id=auth.tenant_id,
                    conversation_id=conversation_id,
                ),
                task_id=task_id,
                execution_id=str(resolved.execution_id or message.execution_id),
                status=str(resolved.status),
                artifacts=list(resolved.artifacts),
                error=result.error_message or result.error,
                goal_id=_execution_goal_id(payload),
                objective_id=_execution_objective_id(payload),
            )
            if updated_task is not None:
                task_status = str(
                    getattr(getattr(updated_task, "status", None), "value", "")
                    or getattr(updated_task, "status", "")
                    or task_status
                )

        projection = self._result_projection_store.save(ExecutionResultProjection(
            execution_id=str(resolved.execution_id or message.execution_id),
            task_id=task_id,
            conversation_id=conversation_id,
            run_id=run_id,
            trace_id=trace_id,
            objective_id=_execution_objective_id(payload),
            status=str(resolved.status),
            task_status=task_status,
            artifacts=list(resolved.artifacts),
            schedule=resolved.schedule,
            next_actions=list(response.next_actions),
            summary=_projection_summary(resolved, response),
            assistant_response=response.model_dump(mode="json"),
        ))
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("projection_persisted", run_id=run_id)
        except Exception:
            pass
        return await self._apply_projection(
            projection,
            response,
            auth=auth,
            result=result,
        )

    async def reconcile(
        self,
        message: ExecutionQueueMessage,
        execution: Any,
        *,
        result: RuntimeResult | None = None,
    ) -> bool:
        status = str(getattr(getattr(execution, "status", ""), "value", getattr(execution, "status", "")))
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            return False
        auth = _auth_from_message(message)
        existing = self._result_projection_store.get(message.execution_id)
        if existing is not None:
            response = AgentResponse.model_validate(existing.assistant_response)
            recovered = RuntimeResult(
                # A RESULT_UNKNOWN result may already have a durable
                # projection from the original queue delivery.  Once the
                # authoritative operation read-back terminally settles its
                # Execution, replay that projection with the Execution truth
                # so Task/Objectives receive their normal completion update.
                # Re-applying the old RESULT_UNKNOWN projection here leaves
                # the Task permanently non-terminal after reconciliation.
                success=status == "COMPLETED",
                status=status,
                run_id=existing.run_id,
                task_id=existing.task_id,
                execution_id=existing.execution_id,
                trace_id=existing.trace_id,
                content=response.message,
                summary=existing.summary,
                artifacts=list(
                    result.artifacts
                    if result is not None and result.artifacts
                    else existing.artifacts
                ),
                schedule=(
                    result.schedule
                    if result is not None and result.schedule is not None
                    else existing.schedule
                ),
                # The old RESULT_UNKNOWN projection may contain a RUNNING
                # presentation step even though the authoritative Execution
                # has now reached a terminal state.  Leave steps empty here
                # so ResultResolver repopulates them from that durable
                # Execution instead of replaying stale UI progress.
                steps=[],
                presentation=existing.assistant_response,
            )
            return await self.complete(
                message,
                recovered,
                auth,
                execution=execution,
            )

        payload = message.payload
        recovered = RuntimeResult(
            success=status == "COMPLETED",
            status=status,
            run_id=str(payload.get("run_id") or ""),
            task_id=str(payload.get("task_id") or getattr(execution, "task_id", "")),
            execution_id=message.execution_id,
            trace_id=str(payload.get("trace_id") or message.trace_id),
            summary=str(payload.get("user_message") or ""),
            error_message=("Execution failed." if status == "FAILED" else ""),
        )
        return await self.complete(
            message,
            recovered,
            auth,
            execution=execution,
        )

    async def _apply_projection(
        self,
        projection: ExecutionResultProjection,
        response: AgentResponse,
        *,
        auth: AuthContext,
        result: RuntimeResult,
    ) -> bool:
        if not await self._conversation_exists(projection, auth=auth):
            # Historical executions may outlive their Conversation. Keep the
            # durable execution projection, but do not recreate or mutate a
            # user-visible conversation that has been removed.
            return False

        try:
            await self._update_conversation_context(projection, auth=auth)
            result_part = _result_part(projection, response)
            existing_messages = await self._conversation_service.list_messages(
                projection.conversation_id,
                user_id=auth.user_id,
                tenant_id=auth.tenant_id,
            )
        except ConversationNotFoundError:
            # The Conversation can be deleted between the existence check and
            # message publication. Treat that race as an idempotent no-op.
            return False
        existing = next(
            (
                item for item in existing_messages
                if item.get("role") == "assistant"
                and (
                    str(item.get("execution_id") or "") == projection.execution_id
                    or str(item.get("trace_id") or "") == projection.trace_id
                )
            ),
            None,
        )
        parts = _merge_result_parts(
            list(existing.get("parts") or []) if existing is not None else [],
            result_part,
        )
        message_content = _aggregate_result_message(parts, response.message)
        published = existing is None
        if existing is None:
            try:
                await self._conversation_service.append_message(
                    projection.conversation_id,
                    user_id=auth.user_id,
                    tenant_id=auth.tenant_id,
                    role="assistant",
                    content=message_content,
                    trace_id=projection.trace_id,
                    parts=parts,
                    run_id=projection.run_id,
                    execution_id=projection.execution_id,
                )
            except ConversationNotFoundError:
                return False
        elif (
            existing.get("content") != message_content
            or existing.get("parts") != parts
        ):
            updater = getattr(self._conversation_service, "update_message_projection", None)
            if updater is not None:
                try:
                    await updater(
                        projection.conversation_id,
                        user_id=auth.user_id,
                        tenant_id=auth.tenant_id,
                        trace_id=projection.trace_id,
                        content=message_content,
                        parts=parts,
                        run_id=projection.run_id,
                        execution_id=str(
                            existing.get("execution_id") or projection.execution_id
                        ),
                    )
                except ConversationNotFoundError:
                    return False

        self._update_run_store(projection, response, result)
        return published

    async def _conversation_exists(
        self,
        projection: ExecutionResultProjection,
        *,
        auth: AuthContext,
    ) -> bool:
        getter = getattr(self._conversation_service, "get_conversation", None)
        if getter is None:
            # Compatibility for narrow test/legacy context adapters. Durable
            # ConversationService implementations always expose get_conversation.
            return True
        conversation = await getter(
            projection.conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        return conversation is not None

    async def _update_conversation_context(
        self,
        projection: ExecutionResultProjection,
        *,
        auth: AuthContext,
    ) -> None:
        if not hasattr(self._conversation_service, "load") or not hasattr(
            self._conversation_service,
            "save_session",
        ):
            return
        snapshot = await self._conversation_service.load(
            projection.conversation_id,
            user_id=auth.user_id,
            tenant_id=auth.tenant_id,
        )
        session = snapshot.session
        session.active_task_id = projection.task_id or session.active_task_id
        draft = _artifact_by_resource_type(projection.artifacts, "DRAFT")
        schedule = _artifact_by_resource_type(projection.artifacts, "SCHEDULE")
        post = _artifact_by_resource_type(projection.artifacts, "POST")
        active = draft or post or schedule
        if active and active.get("artifact_id"):
            session.active_artifact_id = str(active["artifact_id"])
        if draft and draft.get("resource_id"):
            session.active_draft_id = str(draft["resource_id"])
        if schedule and schedule.get("resource_id"):
            session.active_schedule_id = str(schedule["resource_id"])
        if post and post.get("resource_id"):
            session.active_post_id = str(post["resource_id"])
        if projection.status == "COMPLETED":
            session.last_successful_run_id = projection.run_id
        await self._conversation_service.save_session(session)

    def _update_run_store(
        self,
        projection: ExecutionResultProjection,
        response: AgentResponse,
        result: RuntimeResult,
    ) -> None:
        record = dict(self._run_store.get(projection.run_id) or {})
        record.update({
            "run_id": projection.run_id,
            "conversation_id": projection.conversation_id,
            # For a Task-bound projection this compatibility store must show
            # the aggregate Task status, not the status of this one child
            # Execution.  The durable AgentRun convergence remains the
            # lifecycle authority; this local record must not contradict it.
            "status": projection.task_status or projection.status,
            "content": response.message,
            "trace_id": projection.trace_id,
            "execution_id": projection.execution_id,
            "plan_id": result.plan_id,
            "task_id": projection.task_id,
            "steps": list(result.steps),
            "artifacts": list(projection.artifacts),
            "presentation": projection.assistant_response,
            "partial_results": result.partial_results or {},
            "error_code": result.error_code or None,
            "error": result.error_message or result.error or None,
        })
        if result.events:
            record["events"] = list(result.events)
        self._run_store[projection.run_id] = record


def _result_part(
    projection: ExecutionResultProjection,
    response: AgentResponse,
) -> dict[str, Any]:
    return {
        "type": "execution_result",
        "execution": {
            "execution_id": projection.execution_id,
            "task_id": projection.task_id,
            "status": projection.status,
            "task_status": projection.task_status,
            "summary": projection.summary,
            "steps": list(response.steps),
            "business_projection": (
                response.business_projection.model_dump(mode="json")
                if response.business_projection is not None
                else None
            ),
        },
        "artifacts": [
            {
                **item.model_dump(mode="json"),
                "resource_type": _resource_type_from_presentation(item.type),
            }
            for item in response.artifacts
        ],
        "schedule": projection.schedule,
        "next_actions": list(projection.next_actions),
    }


def _merge_result_parts(
    existing_parts: list[dict[str, Any]],
    result_part: dict[str, Any],
) -> list[dict[str, Any]]:
    """Idempotently merge one child Execution into its conversation Turn."""

    execution = result_part.get("execution") or {}
    execution_id = str(execution.get("execution_id") or "")
    merged: list[dict[str, Any]] = []
    replaced = False
    for item in existing_parts:
        if not isinstance(item, dict):
            continue
        current = item.get("execution") or {}
        if (
            item.get("type") == "execution_result"
            and execution_id
            and str(current.get("execution_id") or "") == execution_id
        ):
            merged.append(result_part)
            replaced = True
        else:
            merged.append(dict(item))
    if not replaced:
        merged.append(result_part)
    return merged


def _aggregate_result_message(
    parts: list[dict[str, Any]],
    single_message: str,
) -> str:
    results = [item for item in parts if item.get("type") == "execution_result"]
    if len(results) <= 1:
        return single_message
    lines = [f"本次任务包含 {len(results)} 个子任务："]
    status_labels = {
        "COMPLETED": "已完成",
        "FAILED": "失败",
        "CANCELLED": "已取消",
        "RUNNING": "执行中",
        "QUEUED": "等待执行",
    }
    for index, item in enumerate(results, start=1):
        execution = item.get("execution") or {}
        status = str(execution.get("status") or "")
        summary = str(execution.get("summary") or f"子任务 {index}")
        lines.append(
            f"{index}. {summary}（{status_labels.get(status, '状态已更新')}）"
        )
    return "\n".join(lines)


def _projection_summary(result: RuntimeResult, response: AgentResponse) -> str:
    draft = _artifact_by_resource_type(result.artifacts, "DRAFT")
    return str(
        (draft or {}).get("summary")
        or (draft or {}).get("title")
        or result.summary
        or response.message
    )[:1000]


def _artifact_by_resource_type(
    artifacts: list[dict[str, Any]],
    resource_type: str,
) -> dict[str, Any] | None:
    for artifact in artifacts:
        current = str(
            artifact.get("resource_type")
            or _resource_type_from_presentation(
                str(artifact.get("type") or artifact.get("artifact_type") or "")
            )
            or ""
        )
        if current == resource_type:
            return artifact
    return None


def _resource_type_from_presentation(artifact_type: str) -> str | None:
    normalized = str(artifact_type).upper()
    if normalized in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
        return "DRAFT"
    if normalized in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
        return "SCHEDULE"
    if normalized in {"POST", "PUBLISHED_POST", "PUBLICATION"}:
        return "POST"
    return None


def _execution_objective_id(payload: Mapping[str, Any]) -> str:
    """Resolve the Objective that initiated this Execution (durable correlation).

    The value is the same ``objective_id`` persisted on the PlanExecution at
    submission (the initiating Objective), carried on the queue envelope so the
    completion projection binds a produced Resource to that Objective even when
    the turn has since moved on.  A missing value stays empty: never guess the
    owner from the current/active Objective.
    """

    return str(payload.get("objective_id") or "")


def _execution_goal_id(payload: Mapping[str, Any]) -> str:
    """Resolve the Goal this Execution belongs to (incremental mode)."""

    execution_input = payload.get("execution_input") or {}
    if not isinstance(execution_input, Mapping):
        return ""
    goal_id = str(execution_input.get("goal_id") or "")
    if goal_id:
        return goal_id
    steps = execution_input.get("steps") or []
    if steps and isinstance(steps[0], Mapping):
        return str(steps[0].get("goal_id") or "")
    return ""


def _auth_from_message(message: ExecutionQueueMessage) -> AuthContext:
    payload = message.payload
    identity = payload.get("auth_context") or {}
    user_id = str(identity.get("user_id") or payload.get("user_id") or "")
    tenant_id = str(identity.get("tenant_id") or payload.get("tenant_id") or "")
    if not user_id or not tenant_id:
        raise RuntimeError(
            f"Queued execution {message.execution_id} has no authenticated projection scope"
        )
    return AuthContext(
        user_id=user_id,
        tenant_id=tenant_id,
        roles=[str(role) for role in (identity.get("roles") or [])],
        timezone=str(identity.get("timezone") or payload.get("timezone") or "Asia/Shanghai"),
        raw_access_token="",
    )


__all__ = ["CompletionProjectionCoordinator"]
