"""Phase 3A unified Turn entry point.

TurnCoordinator is the single turn boundary.  It assembles a bounded context,
interprets the Command, resolves target and temporal, runs FastPathGate, and
routes:

    FAST / QUERY / CHAT / CLARIFY  -> FastPathExecutor
    COMPLEX                        -> ActionLoopExecutor

    It owns no Queue / Worker / Lease / Retry / JavaClient / Tool execution / SSE;
    those stay in the Reliable Runtime.  COMPLEX requests are delegated to the
    Objective-driven ActionLoop with the already-interpreted Command.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Mapping
from typing import Any

from greenbook_agent_core.command import (
    CommandInterpreter,
    TargetResolutionStatus,
    TargetResolver,
)
from greenbook_agent_core.command.target import NotFound, is_failed_objective_retry
from greenbook_agent_core.command.models import (
    Command,
    CommandItem,
    ResolvedSemanticItem,
    ResolvedSemanticState,
    TargetReferenceType,
)
from greenbook_agent_core.execution.boundary import TurnExecutionBoundary
from greenbook_agent_core.execution.runtime_result import RuntimeResult
from greenbook_agent_core.execution.temporal_resolver import (
    TemporalResolution,
    TemporalResolver,
)
from greenbook_agent_core.turn import (
    AssembledTurnContext,
    ContextAssembler,
    FastPathDecision,
    FastPathExecutor,
    FastPathGate,
    TurnRequest,
    TurnRoute,
)
from greenbook_agent_core.turn.fast_path_gate import is_non_actionable_query
from greenbook_agent_core.task.semantic_confirmation import (
    canonical_snapshot_hash,
    confirmation_identity,
    confirmation_policy,
    render_confirmation,
)
from greenbook_contracts.tool_contract import SemanticAction

from .conversation_runtime_adapter import (
    _SEMANTIC_ACTION_CAPABILITIES,
    ConversationRuntimeAdapter,
)
from .explicit_resource_admission import admit_explicit_resources

logger = logging.getLogger(__name__)

# Terminal statuses that are always authoritative — never fall back after them.
_TERMINAL_NO_FALLBACK = {
    "COMPLETED",
    "WAITING_EXTERNAL",
    "WAITING_HUMAN",
    "WAITING_APPROVAL",
    "PARTIAL_FAILURE",
}


def _explicit_semantic_operation(command: Command) -> str:
    """Project one explicit operation without reconciling competing facts."""

    operation = str(getattr(command, "semantic_operation", "") or "")
    operation = operation.strip().upper().replace("-", "_").replace(" ", "_")
    if operation:
        return operation
    actions = {
        str((getattr(delta, "desired_changes", None) or {}).get("semantic_action") or "")
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
        for delta in (getattr(command, "task_changes", None) or ())
    }
    actions.discard("")
    return next(iter(actions)) if len(actions) == 1 else ""


def _normalized_capabilities(values: Any) -> list[str]:
    result: list[str] = []
    for value in values or ():
        normalized = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _task_delta_semantic_item(delta: Any) -> tuple[CommandItem, dict[str, Any]] | None:
    """Project one explicit business mutation into the shared semantic items.

    TaskDelta is already the structured WHAT for an existing resource.  The
    semantic snapshot must not drop that outcome merely because CommandItem
    was originally introduced for newly-created deliverables.  This remains a
    projection only: it creates no Task, Objective, plan, or execution step.
    """

    desired = getattr(delta, "desired_changes", None) or {}
    if not isinstance(desired, Mapping):
        return None
    action = str(desired.get("semantic_action") or "").strip().upper().replace("-", "_")
    if not action:
        return None
    # ``SCHEDULE_PUBLISH`` is the capability spelling; the ActionLoop's
    # canonical business action for creating the resulting Schedule is
    # ``CREATE_SCHEDULE``. Keep the semantic item and mutation path aligned.
    if action == "SCHEDULE_PUBLISH":
        action = SemanticAction.CREATE_SCHEDULE.value
    reference = dict(getattr(delta, "target_reference", None) or {})
    capability = _SEMANTIC_ACTION_CAPABILITIES.get(action, "")
    publication_intent = str(
        desired.get("publication_intent")
        or desired.get("publication_mode")
        or ""
    ).strip().upper().replace("-", "_")
    if not publication_intent:
        if action == "PUBLISH_NOW":
            publication_intent = "IMMEDIATE_PUBLISH"
        elif action in {"CREATE_SCHEDULE", "UPDATE_SCHEDULE"}:
            publication_intent = "SCHEDULED_PUBLISH"
    constraints = dict(desired)
    if publication_intent:
        constraints["publication_intent"] = publication_intent
    temporal_text = str(
        desired.get("run_at")
        or desired.get("publish_at")
        or desired.get("scheduled_at")
        or ""
    ).strip()
    topic = str(
        desired.get("topic")
        or desired.get("subject")
        or reference.get("label")
        or reference.get("reference")
        or ""
    ).strip()
    title = str(desired.get("title") or "").strip()
    description = str(
        desired.get("description") or desired.get("instruction") or ""
    ).strip()
    requirements = [description] if description else []
    item = CommandItem(
        title=title,
        topic=topic,
        requirements=requirements,
        operation=action,
        capabilities=[capability] if capability else [],
        temporal_text=temporal_text,
        constraints=constraints,
    )
    return item, reference


def _command_item_covers_delta(item: Any, delta: Any) -> bool:
    """Avoid counting one provider item and its same-target mutation twice.

    Provider responses may identify the same mutation in both ``items`` and
    ``task_changes``.  Prefer structured identity evidence; a natural-language
    label is only the compatibility fallback for older provider payloads.
    """

    reference = getattr(delta, "target_reference", None) or {}
    if not isinstance(reference, Mapping):
        return False
    item_key = str(getattr(item, "item_key", "") or "").strip().casefold()
    change_id = str(getattr(delta, "change_id", "") or "").strip().casefold()
    if item_key and change_id and (
        item_key == change_id
        or item_key.startswith(f"{change_id}_")
        or change_id.startswith(f"{item_key}_")
    ):
        return True
    label = str(
        reference.get("label")
        or reference.get("reference")
        or ""
    ).strip().casefold()
    if not label:
        return False
    haystack = " ".join(
        str(getattr(item, key, "") or "")
        for key in ("title", "topic", "requirements")
    ).casefold()
    return label in haystack


def _command_item_action_matches_delta(item: Any, delta: Any) -> bool:
    """Return whether an item has the delta's canonical action capability."""

    desired = getattr(delta, "desired_changes", None) or {}
    if not isinstance(desired, Mapping):
        return False
    action = str(desired.get("semantic_action") or "").strip().upper()
    action = action.replace("-", "_").replace(" ", "_")
    if action == "SCHEDULE_PUBLISH":
        action = SemanticAction.CREATE_SCHEDULE.value
    capability = _SEMANTIC_ACTION_CAPABILITIES.get(action, "")
    item_capabilities = _normalized_capabilities(
        getattr(item, "capabilities", None) or ()
    )
    item_operation = str(getattr(item, "operation", "") or "").strip().upper()
    item_operation = item_operation.replace("-", "_").replace(" ", "_")
    return bool(
        (capability and capability in item_capabilities)
        or (action and action == item_operation)
    )


# Capabilities proven to complete through the new ActionLoop (real E2E or
# deterministic focused coverage).  A MIGRATED capability that fails in the new
# Runtime must surface its failure, never be silently re-run through legacy.
def _terminal_without_fallback(result: RuntimeResult) -> bool:
    return result.status in _TERMINAL_NO_FALLBACK


class TurnCoordinator:
    """Route one user turn to a Fast Path or the existing Complex Path."""

    def __init__(
        self,
        *,
        context_assembler: ContextAssembler | Any | None = None,
        command_runtime: CommandInterpreter | Any | None = None,
        target_resolver: TargetResolver | Any | None = None,
        temporal_resolver: TemporalResolver | Any | None = None,
        fast_path_gate: FastPathGate | Any | None = None,
        fast_path_executor: FastPathExecutor | Any | None = None,
        complex_path: ConversationRuntimeAdapter | Any | None = None,
        tool_registry: Any = None,
        action_loop_executor: Any | None = None,
        task_manager: Any | None = None,
    ) -> None:
        self._assembler = context_assembler or ContextAssembler()
        self._command_runtime = command_runtime
        self._target_resolver = target_resolver or TargetResolver()
        self._temporal_resolver = temporal_resolver or TemporalResolver()
        self._gate = fast_path_gate or FastPathGate()
        self._complex_path = complex_path
        self._action_loop_executor = action_loop_executor
        self._task_manager = task_manager or getattr(action_loop_executor, "_task_manager", None)
        self._tool_registry = tool_registry or getattr(
            getattr(complex_path, "_tool_registry", None), "list", lambda: ()
        )
        if fast_path_executor is not None:
            self._executor = fast_path_executor
        else:
            tools = _tool_list(self._tool_registry)
            self._executor = FastPathExecutor(
                tool_registry=tools,
                read_handler=self._read_handler,
                write_submitter=self._write_submitter,
                activity_callback=None,
            )

    async def execute(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        message: str,
        history: Any = None,
        session: Any = None,
        timezone: str | None = None,
        run_id: str = "",
        trace_id: str = "",
        mcp: Any = None,
        llm: Any = None,
        model: str = "",
        auth: Any = None,
        idempotency_key: str = "",
        activity_callback: Any = None,
        completion_callback: Any = None,
        focus_task_ids: Any = None,
        command_override: Any = None,
    ) -> RuntimeResult:
        if command_override is not None and not isinstance(command_override, Command):
            if self._complex_path is None or not callable(
                getattr(self._complex_path, "execute", None)
            ):
                return RuntimeResult(
                    success=False,
                    status="FAILED",
                    run_id=run_id or "",
                    trace_id=trace_id or "",
                    execution_path="fast_path",
                    error_code="CANONICAL_RUNTIME_INCOMPLETE",
                )
            result = self._complex_path.execute(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                message=message,
                history=list(history) if history else None,
                session=session,
                timezone=timezone or "Asia/Shanghai",
                run_id=run_id,
                trace_id=trace_id,
                mcp=mcp,
                llm=llm,
                model=model,
                auth=auth,
                idempotency_key=idempotency_key,
                activity_callback=activity_callback,
                completion_callback=completion_callback,
                _command_override=command_override,
            )
            return await result if inspect.isawaitable(result) else result

        run = run_id or message or "turn"
        trace = trace_id or ""
        request = TurnRequest(
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            message=message,
            history=list(history) if history else None,
            timezone=timezone or "Asia/Shanghai",
            session=session,
            run_id=run,
            trace_id=trace,
            focus_task_ids=list(focus_task_ids or []),
            llm=llm,
            model=model,
            auth=auth,
            mcp=mcp,
            idempotency_key=idempotency_key,
            activity_callback=activity_callback,
            completion_callback=completion_callback,
        )
        try:
            try:
                from greenbook_agent_core.observability.run_metrics import record_stage
                record_stage("context_start", run_id=request.run_id)
            except Exception:
                pass
            assembled = await self._assembler.assemble(
                conversation_id=conversation_id,
                user_id=user_id,
                tenant_id=tenant_id,
                timezone=request.timezone,
                session=session,
                history=request.history,
                focus_task_ids=request.focus_task_ids,
                run_id=request.run_id,
                user_input=message,
            )
        except Exception:  # noqa: BLE001 - context assembly is best-effort
            assembled = self._empty_assembled(request)
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("context_ready", run_id=request.run_id)
        except Exception:
            pass

        if self._command_runtime is None:
            return self._fail(request, "CANONICAL_COMMAND_RUNTIME_UNAVAILABLE")
        # Interpreter and Resolver must consume the same scoped projection.
        # The provider receives a sanitized view of this CommandContext; the
        # resolver keeps its canonical metadata and identity-bearing targets.
        command_context = assembled.to_command_context()
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("semantic_start", run_id=request.run_id)
        except Exception:
            pass
        command = (
            command_override
            if isinstance(command_override, Command)
            else await self._command_runtime.interpret(
                message,
                command_context,
                llm=llm,
                model=model,
                run_id=request.run_id,
                turn_id=request.trace_id or request.run_id,
            )
        )
        try:
            from greenbook_agent_core.command.interpreter import _debug_structured_stage
            _debug_structured_stage(
                "turn_command",
                {"items": [item.model_dump(mode="json") for item in (command.items or ())],
                 "item_count": len(command.items or ())},
                run_id=request.run_id,
                turn_id=request.trace_id or request.run_id,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never affect routing
            pass
        # A typed business identity may refer to a resource created in another
        # Conversation. Resolve it through the user-scoped Java/MCP read
        # boundary before TargetResolver sees it. Labels, recency, and
        # conversation-local "recent" entities never enter this path.
        admission = await admit_explicit_resources(
            command,
            existing_candidates=list(command_context.targets),
            mcp=mcp,
            auth=auth,
            session=session,
            trace_id=request.trace_id,
            run_id=request.run_id,
        )
        if admission.failed:
            unresolved = command.model_copy(update={
                "target_resolution": TargetResolutionStatus.NOT_FOUND.value,
                "target_candidates": [],
            })
            request.current_command = unresolved
            return self._clarify_result(
                unresolved,
                assembled,
                request,
                FastPathDecision(route=TurnRoute.CLARIFY, reason="target_unresolved"),
            )
        if admission.candidates:
            scoped_resources = list(assembled.selected_resources or [])
            for candidate in admission.candidates:
                identity = (
                    str(candidate.get("resource_kind") or candidate.get("kind") or "").upper(),
                    str(candidate.get("resource_id") or candidate.get("id") or ""),
                )
                if not any(
                    (
                        str(item.get("resource_kind") or item.get("kind") or "").upper(),
                        str(item.get("resource_id") or item.get("id") or ""),
                    ) == identity
                    for item in scoped_resources
                    if isinstance(item, Mapping)
                ):
                    scoped_resources.append(candidate)
            assembled = assembled.model_copy(update={"selected_resources": scoped_resources})
            command = admission.command
            command_context = assembled.to_command_context()
            request.current_command = command
        target_resolution = await self._resolve_target(
            command, command_context, assembled=assembled
        )
        command, explicit_task_error = await self._materialize_external_resource_task(
            command,
            request,
            target_resolution,
        )
        if explicit_task_error:
            request.current_command = command
            return self._fail(request, explicit_task_error)
        request.current_command = command
        resolved_target = getattr(target_resolution, "target", None)
        resolved_target_kind = getattr(resolved_target, "kind", "")
        resolved_target_kind = str(
            getattr(resolved_target_kind, "value", resolved_target_kind) or ""
        ).upper()
        semantic_state = self._resolve_semantic_state(
            command,
            target_resolution=target_resolution,
            timezone=request.timezone,
        )
        # ResolvedSemanticState is the canonical semantic snapshot consumed by
        # both paths.  Mirror its derived fields back onto the compatibility
        # Command so FastPath/legacy ComplexPath cannot fall back to an LLM
        # boolean or redundant operation/capability decision.
        command = command.model_copy(update={
            "semantic_operation": semantic_state.semantic_operation,
            "required_capabilities": list(semantic_state.capabilities),
            "needs_clarification": semantic_state.clarification_required,
            "resolved_semantics": semantic_state,
        })
        request.current_command = command
        try:
            from greenbook_agent_core.command.interpreter import _debug_structured_stage
            _debug_structured_stage(
                "resolved_semantic_state",
                semantic_state.model_dump(mode="json"),
                run_id=request.run_id,
                turn_id=request.trace_id or request.run_id,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never affect routing
            pass

        # Semantic Confirmation is a pre-admission gate.  The same
        # ActionLoopExecutor preparation path materializes the durable
        # Task/Objectives and mutation revision, but it never enters ActionLoop
        # while the Task is pending confirmation.
        # A Task-level gate can only be evaluated in the production
        # composition that has durable Task ownership.  Lightweight legacy
        # fixtures without a Task manager are not a Task admission boundary;
        # the production wiring fails closed in _semantic_confirmation_result
        # if its Task preparation service is incomplete.
        if self._task_manager is not None and not semantic_state.clarification_required:
            policy = confirmation_policy(command, semantic_state)
            if policy.required:
                return await self._semantic_confirmation_result(
                    command,
                    semantic_state,
                    request,
                    policy_reason=policy.reason,
                )
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("semantic_resolved", run_id=request.run_id)
        except Exception:
            pass
        try:
            from greenbook_agent_core.observability.bus import observability

            observability().record_trace(
                "semantic_state",
                trace_id=request.trace_id,
                conversation_id=request.conversation_id,
                semantic_action=str(semantic_state.semantic_operation or ""),
                status="CAPABILITIES=" + ",".join(
                    str(value) for value in (semantic_state.capabilities or ())
                ),
            )
        except Exception:
            # Semantic trace enrichment must never affect routing.
            pass
        run_at = semantic_state.run_at
        decision = self._gate.decide(
            command,
            target_resolution=target_resolution,
            run_at=run_at,
            semantic_state=semantic_state,
        )
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("route_decided", run_id=request.run_id)
        except Exception:
            pass
        try:
            from greenbook_agent_core.command.interpreter import _debug_structured_stage
            _debug_structured_stage(
                "turn_route",
                {"item_count": len(command.items or ()), "route": str(decision.route),
                 "reason": str(getattr(decision, "reason", ""))},
                run_id=request.run_id,
                turn_id=request.trace_id or request.run_id,
            )
        except Exception:  # noqa: BLE001 - diagnostics must never affect routing
            pass
        decision = self._with_capability(decision)
        self._observe_routing(request, decision)

        # A cross-turn semantic mutation must run through the Objective-aware
        # ActionLoop.  Fast Path has no Objective owner argument, so a Task
        # with several completed Draft/Schedule bindings would either pick a
        # conversation-global resource or leave the Worker without a grounded
        # target.  The existing loop resolves only the selected Objective's
        # ResourceBinding and submits the same Durable Runtime write.
        has_semantic_mutation = any(
            str((getattr(delta, "desired_changes", None) or {}).get(
                "semantic_action", ""
            )).upper()
            for delta in (getattr(command, "task_changes", None) or ())
        )
        if semantic_state.clarification_required:
            return self._clarify_result(command, assembled, request, decision)
        if has_semantic_mutation and command.target_resolution in {
            TargetResolutionStatus.AMBIGUOUS.value,
            TargetResolutionStatus.NOT_FOUND.value,
        }:
            return self._clarify_result(command, assembled, request, decision)
        if has_semantic_mutation:
            return await self._delegate_complex(command, request)

        if decision.route == TurnRoute.COMPLEX:
            return await self._delegate_complex(command, request)

        if decision.route == TurnRoute.CLARIFY:
            return self._clarify_result(command, assembled, request, decision)

        if decision.route == TurnRoute.CHAT:
            return self._chat_result(command, request)

        return await self._executor.execute(
            decision,
            command,
            context=assembled,
            request=request,
            target_resolution=target_resolution,
            run_at=run_at,
            activity_callback=request.activity_callback,
        )

    # ── collaborators ───────────────────────────────────────────────

    @staticmethod
    def _observe_routing(request: Any, decision: Any) -> None:
        try:
            from greenbook_agent_core.observability.bus import observability

            route = str(getattr(decision, "route", "") or "").upper()
            ob = observability()
            ob.fastpath().inc(route=route)
            ob.record_trace(
                "route_" + route.lower(),
                trace_id=str(getattr(request, "trace_id", "") or getattr(request, "run_id", "") or ""),
                conversation_id=str(getattr(request, "conversation_id", "") or ""),
                semantic_action=", ".join(
                    sorted(
                        str(value)
                        for value in (getattr(decision, "semantic_actions", ()) or ())
                        if value
                    )
                ),
                status=route,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _resolve_target(self, command: Command, context: Any, assembled: Any = None) -> Any:
        # TaskDelta carries the actual cross-turn referent.  Resolve it against
        # every Objective in this conversation before the active-task binding
        # can become a Task selection.  The selected Objective supplies its
        # owning Task; active_task_id remains only a weak fallback for an
        # unqualified "continue/edit this" delta.
        delta_resolution = self._resolve_delta_objective_target(command, assembled)
        if delta_resolution is not None:
            command.target_candidates = [
                candidate.model_dump(mode="json")
                for candidate in (getattr(delta_resolution, "candidates", None) or [])
            ]
            if getattr(delta_resolution, "is_resolved", False):
                target = getattr(delta_resolution, "target", None)
                if target is not None:
                    metadata = getattr(target, "metadata", {}) or {}
                    command.resolved_target = {
                        "task_id": getattr(target, "task_id", None) or None,
                        "objective_id": metadata.get("objective_id") or None,
                        "resource_id": getattr(target, "resource_id", None)
                        or metadata.get("resource_id"),
                        "kind": "TASK",
                    }
                    command.target_resolution = TargetResolutionStatus.RESOLVED.value
            else:
                status = getattr(delta_resolution, "status", TargetResolutionStatus.NOT_FOUND)
                command.target_resolution = str(getattr(status, "value", status))
            return delta_resolution
        failed_reference = bool(
            command.target is not None
            and command.target.reference_type == TargetReferenceType.FAILED
        )
        targeted_query = (
            str(getattr(getattr(command, "type", None), "value", command.type)).upper()
            == "QUERY"
            and command.target is not None
        )
        if (
            (not command.requires_target and not failed_reference and not targeted_query)
            or not command.target
        ):
            return None
        try:
            resolution = self._target_resolver.resolve(command, context)
        except Exception:  # noqa: BLE001 - resolver should be deterministic
            resolution = None
        # A mutation whose task_changes already carry concrete resource targets
        # (needs_target_resolution=false) is grounded even when the top-level
        # target reference is a resource id the resolver can't match.  Resolve
        # the owning Task from the assembled context so CANCEL_SCHEDULE /
        # UPDATE_DRAFT don't spuriously ask the user to re-specify the target.
        if (
            resolution is None
            or not getattr(resolution, "is_resolved", False)
        ):
            resolved = self._mutation_owner_resolution(command, assembled)
            if resolved is not None and getattr(resolved, "is_resolved", False):
                target = getattr(resolved, "target", None)
                task_id = str(
                    getattr(target, "task_id", None)
                    or getattr(target, "id", "") or ""
                )
                if task_id:
                    command.resolved_target = {
                        "task_id": task_id,
                        "kind": "TASK",
                    }
                    command.target_resolution = TargetResolutionStatus.RESOLVED.value
                    command.target_candidates = [
                        candidate.model_dump(mode="json")
                        for candidate in (getattr(resolved, "candidates", None) or [])
                    ]
                    return resolved
        # Surface the resolved owner on the Command so fast-path write
        # submission can bind its Execution to the real Task (otherwise the
        # completion projection tries to load a Task by an empty/wrong id).
        if resolution is not None:
            command.target_candidates = [
                candidate.model_dump(mode="json")
                for candidate in (getattr(resolution, "candidates", None) or [])
            ]
            status = getattr(resolution, "status", TargetResolutionStatus.NOT_FOUND)
            command.target_resolution = str(getattr(status, "value", status)).upper()
        if resolution is not None and getattr(resolution, "is_resolved", False):
            target = getattr(resolution, "target", None)
            if target is not None:
                command.resolved_target = {
                    "task_id": getattr(target, "task_id", None) or None,
                    "resource_id": getattr(target, "resource_id", None)
                    or getattr(target, "id", None),
                    "kind": str(
                        getattr(getattr(target, "kind", None), "value", None)
                        or getattr(target, "kind", "") or ""
                    ).upper(),
                }
                command.target_resolution = TargetResolutionStatus.RESOLVED.value
        return resolution

    async def _materialize_external_resource_task(
        self,
        command: Command,
        request: TurnRequest,
        target_resolution: Any,
    ) -> tuple[Command, str]:
        """Bind an admitted cross-Conversation resource to a fresh Task.

        The business resource remains Java-owned.  This creates only the
        ordinary current-Conversation Task projection required by the existing
        Objective admission and then lets the unchanged ActionLoopExecutor
        load it by ``resolved_target.task_id``.  No source Conversation
        session, recent entity, approval, or execution state is copied.
        """

        parameters = dict(getattr(command, "parameters", None) or {})
        external = [
            dict(item)
            for item in (parameters.get("__external_explicit_resource_admission") or [])
            if isinstance(item, Mapping)
        ]
        if not external or not getattr(command, "task_changes", None):
            return command, ""
        if not getattr(target_resolution, "is_resolved", False):
            return command, ""
        mutation_actions = {
            "UPDATE_DRAFT",
            "DELETE_DRAFT",
            "CREATE_SCHEDULE",
            "UPDATE_SCHEDULE",
            "CANCEL_SCHEDULE",
            "DELETE_POST",
            "PUBLISH_NOW",
        }
        if not any(
            str((getattr(change, "desired_changes", None) or {}).get("semantic_action") or "")
            .strip()
            .upper()
            in mutation_actions
            for change in (getattr(command, "task_changes", None) or ())
        ):
            return command, ""

        manager = self._task_manager
        create = getattr(manager, "create_task", None)
        add_resource = getattr(manager, "add_resource", None)
        if not callable(create) or not callable(add_resource):
            return command, "EXPLICIT_RESOURCE_TASK_ADMISSION_UNAVAILABLE"
        goal = str(
            getattr(command, "requested_goal", "")
            or getattr(command, "raw_input", "")
            or "Explicit business resource operation"
        ).strip()
        try:
            task = create(
                conversation_id=request.conversation_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                goal=goal,
                goal_category=str(getattr(command, "goal_category", "") or "GOAL_DRIVEN"),
            )
            task = await task if inspect.isawaitable(task) else task
            task_id = str(getattr(task, "task_id", "") or "") if task is not None else ""
            if not task_id:
                return command, "EXPLICIT_RESOURCE_TASK_ADMISSION_FAILED"
            for item in external:
                admitted_id = str(item.get("resource_id") or item.get("id") or "")
                admitted_kind = str(
                    item.get("resource_kind") or item.get("kind") or ""
                ).upper()
                if not admitted_id or admitted_kind not in {"DRAFT", "SCHEDULE", "POST"}:
                    return command, "EXPLICIT_RESOURCE_TASK_ADMISSION_FAILED"
                updated = add_resource(
                    task_id,
                    resource_id=admitted_id,
                    resource_kind=admitted_kind,
                    title=str(item.get("title") or item.get("label") or ""),
                    status=str(item.get("status") or ""),
                )
                updated = await updated if inspect.isawaitable(updated) else updated
                if updated is None:
                    return command, "EXPLICIT_RESOURCE_TASK_ADMISSION_FAILED"
        except Exception:  # noqa: BLE001 - explicit admission fails closed
            logger.exception(
                "explicit_resource_task_admission_failed conversation_id=%s run_id=%s",
                request.conversation_id,
                request.run_id,
            )
            return command, "EXPLICIT_RESOURCE_TASK_ADMISSION_FAILED"

        next_command = command.model_copy(deep=True)
        next_parameters = dict(next_command.parameters or {})
        all_admitted = []
        for raw in next_parameters.get("__explicit_resource_admission") or ():
            item = dict(raw) if isinstance(raw, Mapping) else {}
            identity = (
                str(item.get("resource_kind") or item.get("kind") or "").upper(),
                str(item.get("resource_id") or item.get("id") or ""),
            )
            if identity in {
                (
                    str(item2.get("resource_kind") or item2.get("kind") or "").upper(),
                    str(item2.get("resource_id") or item2.get("id") or ""),
                )
                for item2 in external
            }:
                item["task_id"] = task_id
            all_admitted.append(item)
        next_parameters["__explicit_resource_admission"] = all_admitted
        next_parameters["__external_explicit_resource_admission"] = [
            {**item, "task_id": task_id} for item in external
        ]
        next_command.parameters = next_parameters
        target = dict(next_command.resolved_target or {})
        target["task_id"] = task_id
        next_command.resolved_target = target
        changes = []
        external_ids = {
            str(item.get("resource_id") or item.get("id") or "") for item in external
        }
        for change in next_command.task_changes or ():
            reference = dict(change.target_reference or {})
            desired = dict(change.desired_changes or {})
            target_ref = desired.get("resource_target")
            target_ref = dict(target_ref) if isinstance(target_ref, Mapping) else {}
            resource_id = str(
                reference.get("resource_id")
                or reference.get("draft_id")
                or reference.get("schedule_id")
                or reference.get("post_id")
                or target_ref.get("resource_id")
                or ""
            )
            if resource_id in external_ids:
                reference["task_id"] = task_id
                target_ref["task_id"] = task_id
                desired["resource_target"] = target_ref
                change = change.model_copy(update={
                    "target_reference": reference,
                    "desired_changes": desired,
                })
            changes.append(change)
        next_command.task_changes = changes
        return next_command, ""

    def _resolve_delta_objective_target(self, command: Command, assembled: Any) -> Any:
        """Resolve explicit TaskDelta references over conversation Objectives.

        This is deliberately a projection over the existing snapshot and
        TargetResolver; it does not introduce a second index or task manager.
        A single turn may contain several deltas owned by different Tasks;
        each delta is resolved independently and the returned value remains a
        compatibility projection for the existing coordinator API.
        """
        if assembled is None or not getattr(command, "task_changes", None):
            return None
        snapshot = getattr(assembled, "snapshot", None)
        selected_tasks = list(getattr(assembled, "selected_tasks", None) or ())
        snapshot_tasks = list(getattr(snapshot, "active_tasks", None) or ())
        # A FAILED_OBJECTIVE_RETRY is explicitly a historical Objective
        # lookup.  The narrowed selected-task view is optimized for ordinary
        # current-turn grounding and may omit an older failed sibling; using
        # it here turns a valid named retry into NOT_FOUND.  Keep the normal
        # scoped view for other deltas, but let failed retries resolve against
        # the bounded conversation snapshot, where terminal status filtering
        # still remains the resolver's responsibility.
        failed_retry_requested = any(
            is_failed_objective_retry(
                change,
                getattr(change, "target_reference", None) or {},
            )
            for change in (getattr(command, "task_changes", None) or ())
        )
        tasks = snapshot_tasks if failed_retry_requested else (
            selected_tasks or snapshot_tasks
        )
        runtime_parameters = dict(getattr(command, "parameters", None) or {})
        admitted_resources = [
            dict(item)
            for item in (runtime_parameters.get("__explicit_resource_admission") or [])
            if isinstance(item, Mapping)
        ]
        external_resource = bool(
            runtime_parameters.get("__external_explicit_resource_admission")
        )
        if not tasks and not admitted_resources:
            return None
        candidates: list[dict[str, Any]] = list(admitted_resources) if external_resource else []
        for task in () if external_resource else tasks:
            task_id = str(task.get("task_id") or "") if isinstance(task, Mapping) else ""
            if not task_id:
                continue
            task_created = str(task.get("created_at") or "")
            task_updated = str(task.get("updated_at") or "")
            resources = list(task.get("resource_index") or ()) if isinstance(task, Mapping) else []
            objective_resource_owner: dict[str, str] = {}
            for objective in (task.get("objectives") or ()) if isinstance(task, Mapping) else ():
                if not isinstance(objective, Mapping):
                    continue
                objective_id = str(objective.get("objective_id") or "")
                for resource_id in objective.get("related_resource_ids") or ():
                    if objective_id and str(resource_id):
                        objective_resource_owner.setdefault(
                            str(resource_id), objective_id
                        )
            resources = [
                {
                    **dict(resource),
                    "objective_id": str(
                        resource.get("objective_id")
                        or objective_resource_owner.get(
                            str(resource.get("resource_id") or "")
                        )
                        or ""
                    ),
                }
                for resource in resources
                if isinstance(resource, Mapping)
            ]
            candidates.append({
                "id": task_id,
                "task_id": task_id,
                "kind": "TASK",
                "label": task.get("goal") or task.get("goal_summary") or "",
                "status": task.get("status"),
                "created_at": task_created,
                "updated_at": task_updated,
                "resource_index": resources,
                "metadata": {"resource_refs": resources},
            })
            for objective in (task.get("objectives") or ()) if isinstance(task, Mapping) else ():
                if not isinstance(objective, Mapping):
                    continue
                objective_id = str(objective.get("objective_id") or "")
                if not objective_id:
                    continue
                constraints = dict(objective.get("constraints") or {})
                candidates.append({
                    "id": objective_id,
                    "goal_id": objective_id,
                    "objective_id": objective_id,
                    "task_id": task_id,
                    "kind": "TASK",
                    "label": objective.get("description") or objective.get("intent") or "",
                    "status": objective.get("status"),
                    "run_at": constraints.get("run_at"),
                    "constraints": constraints,
                    "created_at": task_created,
                    "updated_at": str(objective.get("updated_at") or task_updated),
                    "resource_index": resources,
                    "metadata": {"objective_id": objective_id, "resource_refs": resources},
                })
        if not candidates:
            return None
        def trace(resolution: Any, *, fallback_used: bool = False) -> Any:
            target = getattr(resolution, "target", None)
            metadata = getattr(target, "metadata", {}) or {}
            hints = [
                dict(getattr(change, "target_reference", None) or {})
                for change in command.task_changes or ()
                if getattr(change, "target_reference", None)
            ]
            logger.info(
                "cross_turn_target_resolution conversation_id=%s active_task_id=%s "
                "candidate_task_ids=%s candidate_objective_ids=%s reference_text=%r "
                "reference_hints=%s selected_task_id=%s selected_objective_id=%s "
                "selection_reason=%s fallback_used=%s resource_bindings=%s",
                getattr(snapshot, "conversation_id", ""),
                getattr(snapshot, "active_task_id", ""),
                sorted({str(item.get("task_id") or "") for item in candidates}),
                [str(item.get("objective_id") or "") for item in candidates],
                str(getattr(command, "raw_input", "") or ""),
                hints,
                getattr(target, "task_id", "") if target is not None else "",
                metadata.get("objective_id") if target is not None else "",
                getattr(resolution, "reason", "") if resolution is not None else "",
                fallback_used,
                [
                    {"objective_id": item.get("objective_id"), "resource_id": ref.get("resource_id"), "resource_kind": ref.get("resource_kind")}
                    for item in candidates for ref in (item.get("resource_index") or ())
                    if isinstance(ref, Mapping)
                ],
            )
            return resolution
        resolver = self._target_resolver
        active_task_id = str(
            getattr(getattr(assembled, "snapshot", None), "active_task_id", "") or ""
        )
        resolved: list[Any] = []
        for change in command.task_changes or ():
            desired = getattr(change, "desired_changes", None) or {}
            if not isinstance(desired, Mapping):
                continue
            reference = getattr(change, "target_reference", None) or {}
            if not isinstance(reference, Mapping):
                reference = {}
            # A provider may preserve the existing FAILED-retry marker while
            # omitting desired_changes (or putting the retry semantics in the
            # item/goal).  It still has to pass through the same deterministic
            # 0/1/>1 Objective resolver; otherwise provider-selected context
            # specificity can bypass ambiguity protection entirely.
            failed_retry = is_failed_objective_retry(change, reference)
            if (
                not str(desired.get("semantic_action") or "").strip()
                and not failed_retry
                and not bool(getattr(change, "needs_target_resolution", False))
            ):
                continue
            # Resolve even a missing reference through the same three-state
            # resolver.  A single candidate may be sufficient; multiple
            # candidates must remain AMBIGUOUS.  Do not delegate this boundary
            # to active_task_id/latest focus, which would hide ambiguity.
            if self._derived_resource_owner_missing(change, assembled):
                resolved.append(NotFound(reason="resource_owner_evidence_missing"))
                continue
            value = resolver.resolve_task_delta(
                change,
                candidates,
                active_task_id=active_task_id,
                conversation_focus_task_id=str(
                    getattr(getattr(assembled, "snapshot", None), "active_task_id", "") or ""
                ),
                user_input=str(getattr(command, "raw_input", "") or ""),
            )
            resolved.append(value)
            if getattr(value, "is_resolved", False) and getattr(value, "target", None) is not None:
                target = value.target
                metadata = getattr(target, "metadata", {}) or {}
                objective_id = str(metadata.get("objective_id") or "")
                if not objective_id:
                    resource_id = str(
                        reference.get("resource_id")
                        or reference.get("draft_id")
                        or reference.get("schedule_id")
                        or reference.get("post_id")
                        or ""
                    )
                    for resource in metadata.get("resource_index") or metadata.get("resource_refs") or ():
                        if isinstance(resource, Mapping) and str(resource.get("resource_id") or "") == resource_id:
                            objective_id = str(resource.get("objective_id") or "")
                            break
                resolved_resource_id = str(getattr(target, "resource_id", "") or "")
                reference_kind = str(
                    reference.get("resource_kind")
                    or reference.get("kind")
                    or ""
                ).upper()
                ref = dict(reference)
                if resolved_resource_id and reference_kind in {"DRAFT", "SCHEDULE", "POST"}:
                    # TargetResolver returns the owner Objective for a delta;
                    # carry the same bounded ResourceRef into the mutation so
                    # ActionLoop can construct the typed Java ToolCall.
                    ref["resource_id"] = resolved_resource_id
                    ref[f"{reference_kind.lower()}_id"] = resolved_resource_id
                if objective_id:
                    # Keep the historical Objective used to ground the
                    # natural reference separate from the new mutation
                    # Objective that ActionLoop will allocate for this turn.
                    desired["target_objective_id"] = objective_id
                    desired["objective_id"] = objective_id
                    ref["target_objective_id"] = objective_id
                    ref["objective_id"] = objective_id
                if objective_id or resolved_resource_id:
                    change.target_reference = ref
        if not resolved:
            return None
        if any(not getattr(item, "is_resolved", False) for item in resolved):
            return trace(next(item for item in resolved if not getattr(item, "is_resolved", False)))
        # Each delta has already been resolved against its own reference and
        # carries its owning Objective in ``target_reference`` /
        # ``desired_changes``. A turn may update several Tasks at once. The
        # single returned resolution is only the compatibility projection used
        # by the coordinator; it must not collapse independent resolutions into
        # a turn-wide ambiguity.
        return trace(resolved[0])

    @staticmethod
    def _derived_resource_owner_missing(change: Any, assembled: Any) -> bool:
        """Fail closed when a scoped ResourceRef has no canonical owner proof."""

        derived = getattr(assembled, "derived_context", None)
        resources = list(getattr(derived, "relevant_resources", None) or [])
        if not resources:
            return False
        reference = getattr(change, "target_reference", None) or {}
        if not isinstance(reference, Mapping):
            return False
        desired = getattr(change, "desired_changes", None) or {}
        action = str(desired.get("semantic_action") or "").upper()
        expected_kind = str(
            reference.get("resource_kind")
            or reference.get("kind")
            or {
                "UPDATE_SCHEDULE": "SCHEDULE",
                "CANCEL_SCHEDULE": "SCHEDULE",
                "CREATE_SCHEDULE": "DRAFT",
                "PUBLISH_NOW": "DRAFT",
                "UPDATE_DRAFT": "DRAFT",
                "DELETE_DRAFT": "DRAFT",
                "DELETE_POST": "POST",
            }.get(action, "")
        ).upper()
        explicit_id = str(
            reference.get("resource_id")
            or reference.get("id")
            or reference.get("draft_id")
            or reference.get("schedule_id")
            or reference.get("post_id")
            or ""
        ).strip()
        label = str(
            reference.get("label")
            or reference.get("reference")
            or reference.get("description")
            or ""
        ).strip().casefold()
        matched: list[Mapping[str, Any]] = []
        for resource in resources:
            if not isinstance(resource, Mapping):
                continue
            kind = str(resource.get("resource_kind") or "").upper()
            if expected_kind and kind != expected_kind:
                continue
            identifier = str(resource.get("resource_id") or "").strip()
            semantic = " ".join(
                str(resource.get(key) or "")
                for key in ("semantic_label", "title", "label")
            ).casefold()
            if explicit_id and identifier != explicit_id:
                continue
            if label and label not in semantic:
                continue
            if explicit_id or label:
                matched.append(resource)
        if not matched:
            return False
        return any(
            not str(item.get("owner_objective_id") or "").strip()
            or str(item.get("ownership_evidence") or "").startswith("ambiguous")
            for item in matched
            if str(item.get("lifecycle") or "").upper() != "TERMINAL"
        )

    def _mutation_owner_resolution(self, command: Command, assembled: Any) -> Any:
        """Resolve the owning Task from a mutation's concrete resource targets.

        A mutation whose task_changes already carry a concrete resource id
        (needs_target_resolution=false) is grounded on that resource's owner.
        This reuses the canonical resource->Task ownership lookup in the
        TargetResolver (``resolve_task_delta``) rather than hand-matching the
        resource id, so there is exactly one resource->owner mechanism.
        """
        if assembled is None:
            return None
        tasks = list(
            getattr(assembled, "selected_tasks", None)
            or getattr(getattr(assembled, "snapshot", None), "active_tasks", None)
            or []
        )
        for change in getattr(command, "task_changes", ()) or ():
            if getattr(change, "needs_target_resolution", True):
                continue
            resolution = self._target_resolver.resolve_task_delta(change, tasks)
            if getattr(resolution, "is_resolved", False):
                return resolution
        return None

    def _resolve_temporal(self, command: Command, timezone: str) -> str | None:
        """Compatibility accessor backed by the canonical semantic resolver."""
        return self._resolve_semantic_state(
            command,
            target_resolution=None,
            timezone=timezone,
        ).run_at

    def _resolve_semantic_state(
        self,
        command: Command,
        *,
        target_resolution: Any,
        timezone: str,
    ) -> ResolvedSemanticState:
        """Resolve semantic facts once before routing to either execution path."""

        top_constraints = dict(command.constraints or {})
        delta_time = ""
        for delta in command.task_changes or ():
            desired = getattr(delta, "desired_changes", None)
            if isinstance(desired, Mapping):
                delta_time = str(
                    desired.get("run_at")
                    or desired.get("publish_at")
                    or desired.get("scheduled_at")
                    or ""
                ).strip()
                if delta_time:
                    break
        resolved_target = dict(command.resolved_target or {})
        target_candidates = list(command.target_candidates or [])
        target_reference = (
            command.target.model_dump(mode="json") if command.target is not None else {}
        )
        if target_resolution is not None and getattr(target_resolution, "target", None) is not None:
            target = target_resolution.target
            resolved_target = {
                **resolved_target,
                "task_id": getattr(target, "task_id", None) or None,
                "objective_id": (getattr(target, "metadata", {}) or {}).get("objective_id"),
                "resource_id": getattr(target, "resource_id", None)
                or getattr(target, "id", None),
                "kind": str(getattr(getattr(target, "kind", None), "value", "") or getattr(target, "kind", "") or "").upper(),
            }
        if target_resolution is not None:
            target_candidates = [
                candidate.model_dump(mode="json")
                for candidate in (getattr(target_resolution, "candidates", None) or [])
            ]

        semantic_item_inputs: list[tuple[Any, dict[str, Any], bool]] = [
            (item, dict(target_reference), False) for item in (command.items or ())
        ]
        for delta in command.task_changes or ():
            projected = _task_delta_semantic_item(delta)
            if projected is not None:
                command_items = list(command.items or ())
                covered_by_identity = any(
                    _command_item_covers_delta(item, delta)
                    for item in command_items
                )
                action_matches = [
                    item for item in command_items
                    if _command_item_action_matches_delta(item, delta)
                ]
                # A structured provider item with the same canonical action
                # can represent this TaskDelta even when the target is an
                # opaque identifier and therefore has no label to match.  Do
                # this only for a unique action match: two same-action sibling
                # mutations must remain separate unless their structured keys
                # identify the pairing above.
                if covered_by_identity or len(action_matches) == 1:
                    continue
                item, item_target = projected
                semantic_item_inputs.append((item, item_target, True))

        request_intent = self._publication_intent(top_constraints)
        global_intent = request_intent
        item_intents = {
            intent
            for item, _item_target, _is_delta in semantic_item_inputs
            if (intent := self._publication_intent(dict(getattr(item, "constraints", {}) or {})))
        }
        has_item_publication_ownership = bool(item_intents)
        if not command.items and semantic_item_inputs and not item_intents:
            # A request-wide provider hint such as SCHEDULED_PUBLISH must not
            # turn a title-only/update-draft delta into a publication goal.
            # The delta's explicit desired fields are the owner of that
            # mutation's semantics.
            global_intent = ""
        if len(item_intents) > 1 or (
            global_intent and item_intents and global_intent not in item_intents
        ):
            # A request-wide hint cannot overwrite independent item owners.
            # Preserve the item-level publication facts as MIXED instead.
            global_intent = "MIXED"
        elif not global_intent and item_intents:
            nonempty_item_count = sum(
                bool(self._publication_intent(dict(getattr(item, "constraints", {}) or {})))
                for item, _item_target, _is_delta in semantic_item_inputs
            )
            if len(semantic_item_inputs) == 1 or nonempty_item_count == len(semantic_item_inputs):
                global_intent = next(iter(item_intents))
            else:
                # An item without publication evidence must not inherit the
                # one explicit mode from a sibling item.
                global_intent = "MIXED"
        semantic_operation = _explicit_semantic_operation(command)
        required_capabilities = _normalized_capabilities(command.required_capabilities)
        resolved_items: list[ResolvedSemanticItem] = []
        item_run_ats: list[str] = []
        temporal_resolutions: list[TemporalResolution] = []
        temporal_signatures: list[str] = []
        for item, item_target_reference, is_delta in semantic_item_inputs:
            constraints = dict(getattr(item, "constraints", {}) or {})
            temporal_text = str(getattr(item, "temporal_text", "") or "").strip()
            explicit_time = str(
                constraints.get("run_at")
                or constraints.get("publish_at")
                or constraints.get("scheduled_at")
                or ""
            ).strip()
            if not explicit_time and len(semantic_item_inputs) == 1:
                explicit_time = str(
                    top_constraints.get("run_at")
                    or top_constraints.get("publish_at")
                    or top_constraints.get("scheduled_at")
                    or ""
                ).strip()
            temporal_input = temporal_text or explicit_time
            item_intent = self._publication_intent(constraints)
            if not item_intent and not is_delta and not has_item_publication_ownership:
                # Only a request-level fact may be inherited by an item.  A
                # sibling's item-level fact is never a default for this item.
                item_intent = request_intent
            item_run_at = None
            item_temporal = TemporalResolution(timezone=timezone)
            if temporal_input:
                item_temporal = self._temporal_resolver.resolve_result(
                    temporal_input,
                    constraints=({"type": "TIME", "value": temporal_input},),
                    timezone=timezone,
                    immediate=item_intent in {
                        "IMMEDIATE_PUBLISH",
                        "PUBLISH_NOW",
                        "NOW",
                    },
                )
            if not temporal_input and item_intent in {
                "SCHEDULED_PUBLISH",
                "SCHEDULE",
                "SCHEDULED",
                "FUTURE_PUBLISH",
                "FUTURE",
            }:
                # A scheduled item without a time is still a future
                # requirement.  Represent it explicitly instead of treating
                # the absence of a parser result as temporal NONE.
                item_temporal = TemporalResolution(
                    intent="FUTURE",
                    resolved=False,
                    timezone=timezone,
                    unresolved_reason="schedule_time_missing",
                )
            if not temporal_input and item_intent in {
                "IMMEDIATE_PUBLISH",
                "PUBLISH_NOW",
                "NOW",
            }:
                item_temporal = TemporalResolution(
                    intent="NOW",
                    resolved=True,
                    timezone=timezone,
                )
            if item_temporal.intent != "NONE":
                temporal_resolutions.append(item_temporal)
                temporal_signatures.append(
                    str(item_temporal.run_at or temporal_input or item_temporal.temporal_kind)
                )
            item_run_at = item_temporal.run_at
            if item_run_at:
                item_run_ats.append(str(item_run_at))
                constraints["run_at"] = str(item_run_at)
                constraints["timezone"] = timezone
            if item_temporal.intent != "NONE":
                constraints["temporal_kind"] = item_temporal.temporal_kind
                constraints["temporal_resolved"] = bool(item_temporal.resolved)
                if item_temporal.unresolved_reason:
                    constraints["temporal_unresolved_reason"] = item_temporal.unresolved_reason
            resolved_items.append(ResolvedSemanticItem(
                title=str(getattr(item, "title", "") or ""),
                topic=str(getattr(item, "topic", "") or ""),
                item_key=str(getattr(item, "item_key", "") or ""),
                requirements=list(getattr(item, "requirements", ()) or ()),
                operation=str(getattr(item, "operation", "CREATE") or "CREATE"),
                capabilities=[str(value).upper() for value in (getattr(item, "capabilities", ()) or ())],
                publication_intent=item_intent,
                temporal_text=temporal_text,
                temporal_kind=item_temporal.temporal_kind,
                run_at=str(item_run_at) if item_run_at else None,
                temporal_resolved=bool(item_temporal.resolved),
                dependencies=[str(value) for value in (getattr(item, "dependencies", ()) or ()) if str(value).strip()],
                constraints=constraints,
                target_reference=dict(item_target_reference),
            ))

        run_at = item_run_ats[0] if len(item_run_ats) == 1 else None
        if not resolved_items:
            temporal_input = str(
                top_constraints.get("run_at")
                or top_constraints.get("publish_at")
                or top_constraints.get("scheduled_at")
                or delta_time
                or ""
            ).strip()
            if temporal_input:
                temporal_resolution = self._temporal_resolver.resolve_result(
                    command.requested_goal,
                    constraints=({"type": "TIME", "value": temporal_input},),
                    timezone=timezone,
                )
                temporal_resolutions.append(temporal_resolution)
                run_at = temporal_resolution.run_at
                if run_at:
                    top_constraints["run_at"] = str(run_at)
                    top_constraints["timezone"] = timezone

        target_status = str(getattr(command, "target_resolution", "") or "").upper()
        # ``Command.needs_clarification`` is only candidate evidence at the
        # boundary.  The final boolean comes from structured blocking facts,
        # target cardinality, and TemporalResolver results below.
        clarification_required = bool(command.ambiguity)
        if target_status == TargetResolutionStatus.RESOLVED.value:
            # Provider ambiguity and the initial delta marker are stale once
            # the canonical resolver has uniquely grounded the target.  They
            # are evidence for resolution, not an alternative target truth.
            clarification_required = False
        elif any(bool(getattr(delta, "needs_target_resolution", False)) for delta in command.task_changes or ()):
            clarification_required = True
        clarification_reason = str(command.ambiguity or "")
        if target_status == TargetResolutionStatus.RESOLVED.value:
            clarification_reason = ""
        if target_status in {TargetResolutionStatus.AMBIGUOUS.value, TargetResolutionStatus.NOT_FOUND.value}:
            clarification_required = True
            clarification_reason = "ambiguous_target" if target_status == TargetResolutionStatus.AMBIGUOUS.value else "target_unresolved"
        future_intent = global_intent in {"SCHEDULED_PUBLISH", "FUTURE_PUBLISH", "FUTURE"} or any(
            resolution.intent == "FUTURE" for resolution in temporal_resolutions
        )
        unresolved_future = any(
            resolution.intent == "FUTURE" and not resolution.resolved
            for resolution in temporal_resolutions
        )
        if unresolved_future or (future_intent and not run_at and not item_run_ats):
            clarification_required = True
            if target_status not in {
                TargetResolutionStatus.AMBIGUOUS.value,
                TargetResolutionStatus.NOT_FOUND.value,
            }:
                clarification_reason = "schedule_time_unresolved"

        # ``needs_clarification`` and ``ambiguity`` are provider evidence.
        # They must not turn a structurally empty QUERY/acknowledgement into a
        # durable human wait.  Actionable clauses are excluded by the same
        # shared structural predicate used by FastPathGate.
        if is_non_actionable_query(command):
            clarification_required = False
            clarification_reason = ""

        item_temporal_kinds = {
            resolution.temporal_kind
            for resolution in temporal_resolutions
            if resolution.intent != "NONE"
        }
        if len(set(temporal_signatures)) > 1 or len(item_temporal_kinds) > 1:
            temporal_kind = "MIXED"
        elif item_temporal_kinds:
            temporal_kind = next(iter(item_temporal_kinds))
        elif future_intent:
            temporal_kind = "UNRESOLVED"
        elif global_intent in {"IMMEDIATE_PUBLISH", "PUBLISH_NOW", "NOW"}:
            temporal_kind = "NOW"
        else:
            temporal_kind = "NONE"
        temporal_requirements_resolved = bool(temporal_resolutions) and all(
            resolution.resolved for resolution in temporal_resolutions
        )
        temporal_resolved = (
            temporal_requirements_resolved
            or global_intent in {"IMMEDIATE_PUBLISH", "PUBLISH_NOW", "NOW"}
        ) and not unresolved_future
        top_constraints["temporal_kind"] = temporal_kind
        top_constraints["temporal_resolved"] = temporal_resolved
        return ResolvedSemanticState(
            source_command_id=command.command_id,
            operation=str(command.type.value if hasattr(command.type, "value") else command.type),
            semantic_operation=semantic_operation,
            capabilities=required_capabilities,
            publication_intent=global_intent,
            target_type=str(getattr(command.target, "kind", "") or "").upper(),
            target_reference=target_reference,
            resolved_target=resolved_target,
            target_candidates=target_candidates,
            question=str(getattr(command, "question", "") or "")[:1000],
            temporal_kind=temporal_kind,
            run_at=run_at,
            temporal_resolved=temporal_resolved,
            constraints=top_constraints,
            dependencies=[str(value) for value in (command.references or ()) if value],
            clarification_required=clarification_required,
            clarification_reason=clarification_reason,
            risk=str(command.risk or "").upper(),
            requires_approval=bool(command.is_broad_destructive or str(command.risk or "").upper() in {"DESTRUCTIVE", "BROAD_DESTRUCTIVE"}),
            items=resolved_items,
            objectives=[
                {
                    "title": item.title,
                    "topic": item.topic,
                    "operation": item.operation,
                    "capabilities": list(item.capabilities),
                    "publication_intent": item.publication_intent,
                    "temporal_kind": item.temporal_kind,
                    "temporal_resolved": item.temporal_resolved,
                    "run_at": item.run_at,
                    "constraints": dict(item.constraints),
                }
                for item in resolved_items
            ],
        )

    @staticmethod
    def _publication_intent(constraints: Mapping[str, Any]) -> str:
        for key in ("publication_intent", "publication_mode", "content_state"):
            value = str(constraints.get(key) or "").strip().upper().replace("-", "_")
            if value:
                return value
        if constraints.get("publish_now") is True:
            return "IMMEDIATE_PUBLISH"
        if constraints.get("schedule") is True or constraints.get("publish") is True:
            return "SCHEDULED_PUBLISH"
        return ""

    async def _delegate_complex(self, command: Command, request: TurnRequest) -> RuntimeResult:
        executor = self._action_loop_executor
        run_for_command = getattr(executor, "run_for_command", None)
        if not callable(run_for_command):
            return self._fail(request, "CANONICAL_RUNTIME_INCOMPLETE")
        boundary = TurnExecutionBoundary()
        try:
            result = run_for_command(
                command=command,
                conversation_id=request.conversation_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                trace_id=request.trace_id,
                session=request.session,
                timezone=request.timezone,
                mcp=request.mcp,
                auth=request.auth,
                activity_callback=request.activity_callback,
                completion_callback=request.completion_callback,
                boundary=boundary,
            )
            result = await result if inspect.isawaitable(result) else result
        except Exception:  # noqa: BLE001 - preserve the no-duplicate-write boundary
            logger.exception(
                "turn_action_loop_exception run_id=%s boundary=%s",
                request.run_id, boundary.as_dict(),
            )
            return self._side_effect_failure(request, boundary)
        if _terminal_without_fallback(result) or boundary.can_fallback():
            return result
        logger.warning(
            "turn_action_loop_after_side_effect run_id=%s boundary=%s",
            request.run_id, boundary.as_dict(),
        )
        return self._side_effect_failure(request, boundary)

    async def _semantic_confirmation_result(
        self,
        command: Command,
        semantic_state: ResolvedSemanticState,
        request: TurnRequest,
        *,
        policy_reason: str,
    ) -> RuntimeResult:
        prepare = getattr(self._action_loop_executor, "prepare_for_confirmation", None)
        manager = self._task_manager
        if not callable(prepare) or manager is None:
            # Fail closed: a Task requiring confirmation must never fall
            # through to Fast Path or ActionLoop without a durable gate.
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=request.run_id,
                trace_id=request.trace_id,
                execution_path="semantic_confirmation",
                error_code="SEMANTIC_CONFIRMATION_UNAVAILABLE",
                error_message="Semantic confirmation requires durable Task storage.",
            )
        task = prepare(
            command=command,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            session=request.session,
            run_id=request.run_id,
            turn_id=request.trace_id or request.run_id,
        )
        task = await task if inspect.isawaitable(task) else task
        if task is None:
            return self._fail(request, "SEMANTIC_CONFIRMATION_TASK_UNAVAILABLE")
        snapshot_hash = canonical_snapshot_hash(command, semantic_state, task)
        set_pending = getattr(manager, "set_confirmation_pending", None)
        if not callable(set_pending):
            return self._fail(request, "SEMANTIC_CONFIRMATION_TASK_MANAGER_INCOMPLETE")
        task = set_pending(
            str(getattr(task, "task_id", "") or ""),
            snapshot_hash=snapshot_hash,
            resume_run_id=request.run_id,
        )
        task = await task if inspect.isawaitable(task) else task
        preview = render_confirmation(
            command,
            semantic_state,
            task,
            confirmation_id=confirmation_identity(task),
        )
        preview["policy_reason"] = policy_reason
        return RuntimeResult(
            success=False,
            status="WAITING_HUMAN",
            run_id=request.run_id,
            task_id=str(getattr(task, "task_id", "") or ""),
            trace_id=request.trace_id,
            execution_path="semantic_confirmation",
            error_code="SEMANTIC_CONFIRMATION_REQUIRED",
            error_message="Please confirm the resolved task before execution.",
            content="Please confirm the resolved task before execution.",
            partial_results={"semantic_confirmation": preview},
        )

    def _side_effect_failure(self, request: TurnRequest, boundary: Any) -> RuntimeResult:
        """Controlled outcome when a side effect already started and the loop died."""
        if not (
            getattr(boundary, "side_effect_started", False)
            or getattr(boundary, "operation_submitted", False)
            or getattr(boundary, "result_unknown", False)
        ):
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=request.run_id,
                trace_id=request.trace_id,
                execution_path="action_loop",
                error_code="ACTION_LOOP_NO_PROGRESS",
                error_message="执行在产生副作用前中断，未提交写入。",
                content="执行未提交任何写入，请重试。",
            )
        if boundary.result_unknown:
            return RuntimeResult(
                success=False,
                status="WAITING_EXTERNAL",
                run_id=request.run_id,
                trace_id=request.trace_id,
                execution_path="action_loop",
                error_code="ACTION_LOOP_RESULT_UNKNOWN",
                error_message="操作已提交但结果未知，等待执行结果，不做重复操作。",
                content="操作已提交，等待执行结果。",
            )
        return RuntimeResult(
            success=False,
            status="WAITING_EXTERNAL",
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_path="action_loop",
            error_code="ACTION_LOOP_AFTER_SIDE_EFFECT",
            error_message="操作已提交但执行中断，等待恢复，不重复执行。",
            content="操作已提交，等待执行结果。",
        )

    # ── Fast Path adapters ──────────────────────────────────────────

    async def _read_handler(self, *, tool_name: str, arguments: dict, request: TurnRequest):
        import time
        started_at = time.perf_counter()
        adapter = self._complex_path
        if adapter is None or not callable(getattr(adapter, "execute_fast_path_read", None)):
            return {
                "ok": False,
                "code": "FAST_READ_HANDLER_UNAVAILABLE",
                "message": "Fast Path read boundary is unavailable.",
            }
        result = adapter.execute_fast_path_read(
            tool_name=tool_name,
            arguments=arguments,
            user_request=request.message,
            synthesis_requested=(
                str(getattr(getattr(request, "current_command", None), "semantic_operation", "") or "")
                .strip().upper()
                in {"SUMMARIZE", "SUMMARIZE_POST", "SUMMARIZE_CONTENT"}
            ),
            llm=request.llm,
            model=request.model,
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            run_id=request.run_id,
            trace_id=request.trace_id,
            session=request.session,
            auth=request.auth,
            mcp=request.mcp,
        )
        result = await result if inspect.isawaitable(result) else result
        try:
            from greenbook_agent_core.observability.run_metrics import record_tool
            record_tool(round((time.perf_counter() - started_at) * 1000), run_id=request.run_id)
        except Exception:
            pass
        try:
            from greenbook_agent_core.observability.bus import observability

            result_status = (
                getattr(result, "status", None)
                or result.get("code")
                if isinstance(result, Mapping)
                else getattr(result, "status", None)
            )
            observability().record_trace(
                "mcp_call",
                trace_id=request.trace_id,
                conversation_id=request.conversation_id,
                semantic_action=tool_name,
                status=str(result_status or "COMPLETED"),
                latency_ms=round((time.perf_counter() - started_at) * 1000),
            )
        except Exception:
            # Trace enrichment must never change the read result.
            pass
        return result

    async def _write_submitter(
        self,
        *,
        tool_name: str,
        arguments: dict,
        capability: str,
        semantic_action: str,
        command: Command,
        request: TurnRequest,
    ):
        import time
        started_at = time.perf_counter()
        adapter = self._complex_path
        if adapter is None or not callable(getattr(adapter, "submit_fast_path_write", None)):
            return RuntimeResult(
                success=False,
                status="FAILED",
                run_id=request.run_id,
                trace_id=request.trace_id,
                execution_path="fast_path",
                error_code="FAST_WRITE_SUBMITTER_UNAVAILABLE",
            )
        # Bind the Execution to the resolved owning Task so completion
        # projection can load it; a modify/cancel write must not run detached.
        task_id = str(
            (getattr(command, "resolved_target", None) or {}).get("task_id") or ""
        )
        try:
            result = adapter.submit_fast_path_write(
                tool_name=tool_name,
                arguments=arguments,
                capability=capability,
                semantic_action=semantic_action,
                command=command,
                conversation_id=request.conversation_id,
                user_id=request.user_id,
                tenant_id=request.tenant_id,
                run_id=request.run_id,
                trace_id=request.trace_id,
                session=request.session,
                timezone=request.timezone,
                mcp=request.mcp,
                llm=request.llm,
                model=request.model,
                auth=request.auth,
                completion_callback=request.completion_callback,
                activity_callback=request.activity_callback,
                task_id=task_id,
            )
            result = await result if inspect.isawaitable(result) else result
            try:
                from greenbook_agent_core.observability.run_metrics import record_tool
                record_tool(round((time.perf_counter() - started_at) * 1000), run_id=request.run_id)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - a detached reference must not crash the turn
            logger.warning(
                "fast_path write submission failed run_id=%s tool=%s task_id=%s resolved=%s reason=%s",
                request.run_id, tool_name, task_id,
                (getattr(command, "resolved_target", None) or {}), exc,
            )
            return RuntimeResult(
                success=False,
                status="WAITING_HUMAN",
                run_id=request.run_id,
                trace_id=request.trace_id,
                execution_path="fast_path",
                error_code="TARGET_CLARIFICATION_REQUIRED",
                error_message="请明确要修改的是哪一项内容，再告诉我一次。",
                content="请明确要修改的是哪一项内容，再告诉我一次。",
            )
        return result

    def _capability_requires_legacy(self, command: Command) -> bool:
        """True only when the command needs an UNMIGRATED capability.

        MIGRATED capabilities must never fall back to the old Runtime — a real
        new-path bug should surface as NEW_RUNTIME_FAILED, not be masked by an
        old AgentLoop COMPLETED.  A command with no declared capability is
        treated as migrated/new-path (no generic legacy fallback).
        """
        caps = [str(c).upper() for c in (getattr(command, "required_capabilities", None) or ())]
        if not caps:
            return False
        return False

    def _with_capability(self, decision: FastPathDecision) -> FastPathDecision:
        if decision.capability or not decision.semantic_actions:
            return decision
        action = decision.semantic_actions[0]
        capability = _SEMANTIC_ACTION_CAPABILITIES.get(action)
        if not capability:
            return decision
        return decision.model_copy(update={"capability": capability})

    # ── result helpers ──────────────────────────────────────────────

    def _clarify_result(
        self,
        command: Command,
        assembled: AssembledTurnContext,
        request: TurnRequest,
        decision: FastPathDecision,
    ) -> RuntimeResult:
        candidate_values = list(
            (command.resolved_semantics.target_candidates
             if command.resolved_semantics is not None
             else command.target_candidates)
            or []
        )
        reason = _failed_retry_clarification(command, candidate_values) or command.ambiguity or (
            "我还不能确定你想修改哪一项任务，请指定一下。"
            if decision.reason in {"ambiguous_target", "target_unresolved"}
            else "Please clarify the requested outcome."
        )
        return RuntimeResult(
            success=False,
            status="WAITING_HUMAN",
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_path="fast_path",
            error_code=_clarify_code(decision.reason),
            error_message=reason,
            content=reason,
            partial_results={
                "clarification": {
                    "reason": reason,
                    "command": command.model_dump(mode="json"),
                    # Consume the canonical resolver evidence.  Do not rerun
                    # target matching or hand the clarification layer every
                    # resource in the context after filtering.
                    "candidates": candidate_values,
                }
            },
        )

    def _chat_result(self, command: Command, request: TurnRequest) -> RuntimeResult:
        reply = command.requested_goal or request.message
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_path="fast_path",
            content=f"已收到：{reply}",
            summary="已收到你的消息。",
        )

    def _empty_assembled(self, request: TurnRequest) -> AssembledTurnContext:
        from greenbook_agent_core.context.models import ContextSnapshot

        return AssembledTurnContext(
            conversation_id=request.conversation_id,
            user_id=request.user_id,
            tenant_id=request.tenant_id,
            timezone=request.timezone,
            snapshot=ContextSnapshot(conversation_id=request.conversation_id),
        )

    def _fail(self, request: TurnRequest, code: str, message: str = "") -> RuntimeResult:
        return RuntimeResult(
            success=False,
            status="FAILED",
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_path="fast_path",
            error_code=code,
            error_message=message or code,
        )


def _clarify_code(reason: str) -> str:
    return {
        "ambiguous_target": "AMBIGUOUS_TARGET",
        "target_unresolved": "TARGET_CLARIFICATION_REQUIRED",
        "schedule_time_unresolved": "SCHEDULE_TIME_REQUIRED",
        "write_parameters_incomplete": "WRITE_PARAMETERS_REQUIRED",
    }.get(reason, "COMMAND_CLARIFICATION_REQUIRED")


def _failed_retry_clarification(
    command: Command,
    candidates: list[dict[str, Any]],
) -> str:
    """Render a safe business clarification for an ambiguous failed retry."""

    retry_marker = any(
        is_failed_objective_retry(
            delta,
            getattr(delta, "target_reference", None) or {},
        )
        for delta in (getattr(command, "task_changes", None) or ())
    )
    if not retry_marker:
        return ""
    labels = [
        str(candidate.get("label") or candidate.get("title") or "").strip()
        for candidate in candidates
        if isinstance(candidate, Mapping)
        and str(candidate.get("status") or "").upper() == "FAILED"
    ]
    labels = list(dict.fromkeys(label for label in labels if label))
    if len(labels) < 2:
        return ""
    count = "两个" if len(labels) == 2 else str(len(labels))
    return f"存在{count}失败的内容任务（{'、'.join(labels)}），你想重试哪一个？"


def _tool_list(registry: Any) -> list[Any]:
    if registry is None:
        return []
    # The MCP service exposes a module (not an instance) whose metadata registry
    # yields ToolMetadata objects that the FastPathExecutor can match on.
    # Prefer metadata to the raw ToolContract list so capability/semantic-action
    # resolution works for every fast-path write.
    for method in ("list", "list_tool_metadata", "list_tools"):
        call = getattr(registry, method, None)
        if callable(call):
            try:
                return list(call())
            except TypeError:
                continue
    if isinstance(registry, (list, tuple)):
        return list(registry)
    return []


__all__ = ["TurnCoordinator"]
