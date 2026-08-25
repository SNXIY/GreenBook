"""Write deterministic UserActivity projections to the durable store."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from greenbook_contracts.tool_result import ResourceRef

from .projector import ProjectedUserActivity, UserActivityProjector
from .store import UserActivityStoreProtocol


class UserActivityPublisher:
    """Small composition boundary around a pure projector and durable store.

    The publisher never invokes a tool or changes a Task/Execution.  Failed
    activity persistence is deliberately surfaced to its caller so a process
    does not falsely advertise a stream it failed to persist.
    """

    def __init__(
        self,
        store: UserActivityStoreProtocol,
        *,
        projector: UserActivityProjector | None = None,
    ) -> None:
        self._store = store
        self._projector = projector or UserActivityProjector()

    def publish_started(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        semantic_action: str | None = None,
        capability: str | None = None,
        tool_name: str | None = None,
        source_key: str,
        created_at: str | None = None,
    ) -> Any | None:
        projected = self._projector.project_started(
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
            objective_id=objective_id,
            semantic_action=semantic_action,
            capability=capability,
            tool_name=tool_name,
            source_key=source_key,
            created_at=created_at,
        )
        return self._persist(projected, user_id=user_id, tenant_id=tenant_id)

    def publish_result(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        result: Any,
        semantic_action: str | None = None,
        capability: str | None = None,
        tool_name: str | None = None,
        source_key: str,
        created_at: str | None = None,
    ) -> Any | None:
        projected = self._projector.project_result(
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
            objective_id=objective_id,
            result=result,
            semantic_action=semantic_action,
            capability=capability,
            tool_name=tool_name,
            source_key=source_key,
            created_at=created_at,
        )
        return self._persist(projected, user_id=user_id, tenant_id=tenant_id)

    def publish_runtime_event(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str | None,
    ) -> Any | None:
        """Project adapter/AgentLoop/Worker callback facts into one event."""

        facts = dict(payload)
        task_id = _text(facts.get("task_id"))
        objective_id = _text(facts.get("objective_id") or facts.get("goal_id"))
        semantic_action = _text(
            facts.get("business_action") or facts.get("semantic_action")
        )
        capability = _text(facts.get("capability"))
        tool_name = _text(facts.get("tool_name"))
        source_key = _source_key(
            run_id=run_id,
            task_id=task_id,
            objective_id=objective_id,
            payload=facts,
            semantic_action=semantic_action or capability or tool_name,
        )
        normalized = str(event_type or "").upper()
        # ``SEMANTIC_ACTION_SELECTED`` is a planning fact, not proof that a
        # tool started.  Showing it as user progress would fabricate work for
        # queued, rejected, or later-replanned actions.  Only the Runtime
        # invocation boundary may publish an in-progress business activity.
        if normalized in {"RUNTIME_TOOL_STARTED", "TOOL_STARTED"}:
            return self.publish_started(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=run_id,
                task_id=task_id,
                objective_id=objective_id,
                semantic_action=semantic_action,
                capability=capability,
                tool_name=tool_name,
                source_key=source_key,
                created_at=_text(facts.get("created_at")),
            )
        if normalized in {"ACTION_COMPLETED", "RUNTIME_TOOL_COMPLETED", "TOOL_COMPLETED"}:
            result = _mapping(facts.get("result"))
            # Existing direct AgentLoop events carry result under ``result``;
            # Worker signals may use the whole payload as the result envelope.
            if not result:
                result = facts
            result = {
                **result,
                "semantic_action": semantic_action or result.get("semantic_action"),
                "capability": capability or result.get("capability"),
                "tool_name": tool_name or result.get("tool_name"),
            }
            return self.publish_result(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=run_id,
                task_id=task_id,
                objective_id=objective_id,
                result=result,
                semantic_action=semantic_action,
                capability=capability,
                tool_name=tool_name,
                source_key=source_key,
                created_at=_text(facts.get("completed_at") or facts.get("created_at")),
            )
        return None

    def publish_runtime_result(
        self,
        result: Any,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str | None = None,
    ) -> list[Any]:
        """Project terminal/waiting RuntimeResult facts from a Worker/API path.

        Per-step ``activity_records`` are the authoritative source when
        present.  The aggregate Runtime status is used only for an explicit
        clarification, approval, unknown outcome, or failure with no more
        specific business operation evidence.
        """

        facts = _mapping(result)
        resolved_run_id = _text(facts.get("run_id")) or run_id
        task_id = _text(facts.get("task_id"))
        status = _text(facts.get("status")).upper()
        error_code = _text(facts.get("error_code"))
        partial = _mapping(facts.get("partial_results"))
        emitted: list[Any] = []

        for index, record in enumerate(facts.get("activity_records") or []):
            item = _mapping(record)
            if not item:
                continue
            item_task_id = _text(item.get("task_id")) or task_id
            item_goal_id = _text(item.get("objective_id") or item.get("goal_id"))
            source_key = _source_key(
                run_id=resolved_run_id,
                task_id=item_task_id,
                objective_id=item_goal_id,
                payload=item,
                semantic_action=_text(item.get("semantic_action") or item.get("capability")),
                fallback=f"runtime-result:{index}",
            )
            started = self.publish_started(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=resolved_run_id,
                task_id=item_task_id,
                objective_id=item_goal_id,
                semantic_action=_text(item.get("semantic_action")),
                capability=_text(item.get("capability")),
                tool_name=_text(item.get("tool_name")),
                source_key=source_key,
                created_at=_text(item.get("started_at")),
            )
            if started is not None:
                emitted.append(started)
            completed = self.publish_result(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                run_id=resolved_run_id,
                task_id=item_task_id,
                objective_id=item_goal_id,
                result=item.get("result") or item,
                semantic_action=_text(item.get("semantic_action")),
                capability=_text(item.get("capability")),
                tool_name=_text(item.get("tool_name")),
                source_key=source_key,
                created_at=_text(item.get("completed_at")),
            )
            if completed is not None:
                emitted.append(completed)

        semantic_confirmation = _mapping(partial.get("semantic_confirmation"))
        if semantic_confirmation:
            emitted.append(self._persist(
                self._projector.project_semantic_confirmation(
                    conversation_id=conversation_id,
                    run_id=resolved_run_id,
                    task_id=task_id or _text(semantic_confirmation.get("task_id")),
                    confirmation=semantic_confirmation,
                    source_key=f"{resolved_run_id or conversation_id}:waiting",
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            ))
            return [item for item in emitted if item is not None]

        clarification = _mapping(partial.get("clarification"))
        if error_code == "AMBIGUOUS_TARGET" or clarification:
            emitted.append(self._persist(
                self._projector.project_clarification(
                    conversation_id=conversation_id,
                    run_id=resolved_run_id,
                    task_id=task_id,
                    objective_id=None,
                    clarification=clarification,
                    source_key=f"{resolved_run_id or conversation_id}:waiting",
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            ))
            return [item for item in emitted if item is not None]

        approval = _mapping(facts.get("approval_data") or facts.get("approval"))
        if approval or status == "WAITING_APPROVAL" or (
            status == "WAITING_HUMAN" and _text(facts.get("approval_id"))
        ):
            if facts.get("approval_id") and "approval_id" not in approval:
                approval["approval_id"] = _text(facts.get("approval_id"))
            emitted.append(self._persist(
                self._projector.project_approval(
                    conversation_id=conversation_id,
                    run_id=resolved_run_id,
                    task_id=task_id,
                    objective_id=_text(approval.get("goal_id")),
                    approval=approval,
                    source_key=f"{resolved_run_id or conversation_id}:waiting",
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            ))
            return [item for item in emitted if item is not None]

        if error_code == "RESULT_UNKNOWN" or status == "RESULT_UNKNOWN":
            emitted.append(self._persist(
                self._projector.project_unknown(
                    conversation_id=conversation_id,
                    run_id=resolved_run_id,
                    task_id=task_id,
                    objective_id=None,
                    resource_ref=_resource_ref_from_result(facts),
                    source_key=f"{resolved_run_id or conversation_id}:result",
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            ))
        elif status == "FAILED" and not facts.get("activity_records"):
            emitted.append(self._persist(
                self._projector.project_failed(
                    conversation_id=conversation_id,
                    run_id=resolved_run_id,
                    task_id=task_id,
                    objective_id=None,
                    source_key=f"{resolved_run_id or conversation_id}:result",
                    code=error_code,
                ),
                user_id=user_id,
                tenant_id=tenant_id,
            ))
        return [item for item in emitted if item is not None]

    def publish_reconciling(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        run_id: str | None,
        task_id: str | None,
        objective_id: str | None,
        resource_ref: ResourceRef | None,
        source_key: str,
    ) -> Any:
        return self._persist(
            self._projector.project_reconciling(
                conversation_id=conversation_id,
                run_id=run_id,
                task_id=task_id,
                objective_id=objective_id,
                resource_ref=resource_ref,
                source_key=source_key,
            ),
            user_id=user_id,
            tenant_id=tenant_id,
        )

    def _persist(
        self,
        projected: ProjectedUserActivity | None,
        *,
        user_id: str,
        tenant_id: str,
    ) -> Any | None:
        if projected is None:
            return None
        return self._store.append(
            projected.event,
            user_id=user_id,
            tenant_id=tenant_id,
            dedupe_key=projected.dedupe_key,
        )


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        rendered = dump(mode="json")
        return dict(rendered) if isinstance(rendered, Mapping) else {}
    return dict(getattr(value, "__dict__", {}) or {})


def _text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


def _source_key(
    *,
    run_id: str | None,
    task_id: str | None,
    objective_id: str | None,
    payload: Mapping[str, Any],
    semantic_action: str | None,
    fallback: str = "",
) -> str:
    explicit = _text(payload.get("activity_key") or payload.get("operation_id"))
    if explicit:
        return explicit
    execution_id = _text(payload.get("execution_id"))
    step_id = _text(payload.get("step_id"))
    invocation_id = _text(payload.get("invocation_id") or payload.get("tool_call_id"))
    if execution_id and step_id:
        return f"execution:{execution_id}:step:{step_id}"
    if invocation_id:
        return f"invocation:{invocation_id}"
    return ":".join([
        "run",
        str(run_id or "conversation"),
        str(task_id or "task"),
        str(objective_id or "objective"),
        str(semantic_action or fallback or "activity"),
    ])


def _resource_ref_from_result(facts: Mapping[str, Any]) -> ResourceRef | None:
    for item in facts.get("artifacts") or []:
        value = _mapping(item)
        resource_id = _text(value.get("resource_id"))
        kind = _text(value.get("resource_type") or value.get("artifact_type"))
        if resource_id and kind:
            return ResourceRef(
                ref=f"{kind.lower()}:{resource_id}",
                kind=kind,
                resource_id=resource_id,
            )
    return None


__all__ = ["UserActivityPublisher"]
