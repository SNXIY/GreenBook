"""Fast Path execution for single reads and explicit writes.

The executor performs no Agent reasoning.  Reads run the selected tool through
the injected read handler and project the *real* response.  Writes pass
ToolPolicyGate, then hand the single-step plan to the injected durable
``write_submitter`` — the same Worker -> Java -> VerificationEvidence ->
OperationReceipt -> UserActivity pipeline as the Complex Path.  No activity is
fabricated; activity is emitted only from real tool responses and durable
submission results.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ..command.models import Command
from ..command.target import TargetCandidate, TargetResolution
from ..execution.runtime_result import RuntimeResult
from ..toolruntime.policy import ToolExecutionMode, ToolPolicyGate
from .models import AssembledTurnContext, FastPathDecision, TurnRequest, TurnRoute

# How a canonical semantic action names its primary business-resource argument.
_RESOURCE_ARG: dict[str, str] = {
    "UPDATE_DRAFT": "draft_id",
    "DELETE_DRAFT": "draft_id",
    "DELETE_POST": "post_id",
    "GET_DRAFT": "draft_id",
    "PUBLISH_NOW": "draft_id",
    "UPDATE_SCHEDULE": "schedule_id",
    "CANCEL_SCHEDULE": "schedule_id",
    "GET_SCHEDULE": "schedule_id",
    "GET_POST": "post_id",
}


class FastPathExecutor:
    """Execute a FastPathDecision against injected IO collaborators."""

    def __init__(
        self,
        *,
        tool_registry: Iterable[Any] | None = None,
        read_handler: Callable[..., Any] | None = None,
        write_submitter: Callable[..., Any] | None = None,
        activity_callback: Any = None,
        policy_gate: ToolPolicyGate | None = None,
        permission_scopes: Iterable[str] = (),
    ) -> None:
        self._tools = list(tool_registry or ())
        self._read_handler = read_handler
        self._write_submitter = write_submitter
        self._activity_callback = activity_callback
        self._policy_gate = policy_gate or ToolPolicyGate()
        self._permission_scopes = tuple(permission_scopes)

    async def execute(
        self,
        decision: FastPathDecision,
        command: Command,
        *,
        context: AssembledTurnContext,
        request: TurnRequest,
        target_resolution: TargetResolution | None = None,
        run_at: str | None = None,
        activity_callback: Any = None,
    ) -> RuntimeResult:
        tool = self._resolve_tool(decision)
        arguments = self._build_arguments(
            decision,
            command,
            target=target_resolution.target if target_resolution else None,
            session=request.session,
            run_at=run_at,
        )
        # Provider/interpreter projections may carry explanatory fields that
        # are useful in semantic evidence but are not inputs to the selected
        # MCP tool.  The ToolContract schema is the execution boundary: keep
        # only declared properties and let the real validator reject missing
        # required values.  Never guess or rename a resource here.
        arguments = self._filter_arguments_to_tool_schema(tool, arguments)
        if decision.route == TurnRoute.QUERY:
            return await self._execute_read(
                decision, tool, arguments, context=context, request=request,
                activity_callback=activity_callback,
            )
        return await self._execute_write(
            decision, tool, arguments, command=command, context=context, request=request,
            activity_callback=activity_callback,
        )

    # ── read ─────────────────────────────────────────────────────────

    async def _execute_read(
        self,
        decision: FastPathDecision,
        tool: Any,
        arguments: dict[str, Any],
        *,
        context: AssembledTurnContext,
        request: TurnRequest,
        activity_callback: Any = None,
    ) -> RuntimeResult:
        handler = self._read_handler
        if handler is None:
            return self._fail(request, "FAST_READ_HANDLER_UNAVAILABLE")
        try:
            value = handler(tool_name=tool.name, arguments=arguments, request=request)
            value = await value if inspect.isawaitable(value) else value
        except Exception as exc:  # noqa: BLE001 - projected as a user result
            return self._fail(
                request,
                "FAST_READ_FAILED",
                str(exc) or "Fast Path read failed.",
            )
        result = self._read_result(request, tool, value)
        await self._emit(
            "FAST_READ",
            self._read_payload(tool, arguments, value),
            context=context,
            task_id=request.run_id,
            activity_callback=activity_callback,
        )
        return result

    # ── write ────────────────────────────────────────────────────────

    async def _execute_write(
        self,
        decision: FastPathDecision,
        tool: Any,
        arguments: dict[str, Any],
        *,
        command: Command,
        context: AssembledTurnContext,
        request: TurnRequest,
        activity_callback: Any = None,
    ) -> RuntimeResult:
        if tool is None:
            return self._fail(request, "FAST_TOOL_METADATA_MISSING")
        policy = self._policy_gate.evaluate(
            tool,
            scopes=self._permission_scopes,
            approval_granted=bool(getattr(request, "approval_granted", False)),
            multi_step=False,
            context={"conversation_id": context.conversation_id},
        )
        if not policy.allowed:
            if policy.mode == ToolExecutionMode.WAITING_HUMAN:
                # The policy gate is only an admission check.  Approval-gated
                # writes must still enter the canonical Runtime so it can
                # materialize PlanExecution -> WAITING_APPROVAL and let the
                # existing ApprovalRuntimeService persist the durable request.
                # Calling the same submitter also preserves the execution
                # identity that approval resume will requeue.
                pass
            else:
                return self._fail(request, "TOOL_POLICY_DENIED", policy.reason)
        submitter = self._write_submitter
        if submitter is None:
            return self._fail(request, "FAST_WRITE_SUBMITTER_UNAVAILABLE")
        try:
            value = submitter(
                tool_name=tool.name,
                arguments=arguments,
                capability=decision.capability,
                semantic_action=next(iter(decision.semantic_actions), ""),
                command=command,
                request=request,
            )
            value = await value if inspect.isawaitable(value) else value
        except Exception as exc:  # noqa: BLE001 - projected as a user result
            return self._fail(
                request,
                "FAST_WRITE_SUBMISSION_FAILED",
                str(exc) or "Fast Path write submission failed.",
            )
        return self._submission_result(value, request)

    # ── argument / tool resolution ───────────────────────────────────

    def _resolve_tool(self, decision: FastPathDecision) -> Any | None:
        if not self._tools:
            return None
        action = next(iter(decision.semantic_actions), "")
        capability = decision.capability
        for tool in self._tools:
            tool_action = str(
                getattr(getattr(tool, "semantic_action", None), "value", "") or ""
            ).upper()
            if tool_action and tool_action == action:
                return tool
        for tool in self._tools:
            capabilities = {
                str(item).upper() for item in getattr(tool, "capabilities", ()) or ()
            }
            if (capability and capability in capabilities) or (
                action and action in capabilities
            ):
                return tool
        return None

    def _build_arguments(
        self,
        decision: FastPathDecision,
        command: Command,
        *,
        target: TargetCandidate | None,
        session: Any,
        run_at: str | None,
    ) -> dict[str, Any]:
        action = next(iter(decision.semantic_actions), "")
        arguments: dict[str, Any] = {}
        for source in (command.entities, command.parameters, command.constraints):
            if isinstance(source, Mapping):
                for key, item in source.items():
                    if key in {
                        "temporal_text", "publish_at", "scheduled_at", "publish_time",
                        "temporal_base", "run_at",
                    }:
                        continue
                    if key not in arguments and item not in (None, "", []):
                        arguments[key] = item
        # Mutation fields carried in a TaskDelta's desired_changes.
        for delta in command.task_changes or ():
            desired = delta.desired_changes if isinstance(delta.desired_changes, Mapping) else {}
            for key, item in desired.items():
                if key in {
                    "semantic_action", "run_at", "temporal_base", "temporal_text",
                    "publish_at", "scheduled_at", "publish_time",
                }:
                    continue
                if key not in arguments and item not in (None, "", []):
                    arguments[key] = item
        resource_arg = _RESOURCE_ARG.get(action)
        if resource_arg:
            resource_id = self._resource_id(resource_arg, target, session)
            if resource_id and not arguments.get(resource_arg):
                arguments[resource_arg] = resource_id
        if action in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE", "PUBLISH_NOW"} and run_at:
            arguments.setdefault("run_at", run_at)
        return arguments

    @staticmethod
    def _filter_arguments_to_tool_schema(
        tool: Any,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        schema = getattr(tool, "input_schema", None)
        if isinstance(schema, type):
            allowed = set(getattr(schema, "model_fields", {}) or {})
        elif isinstance(schema, Mapping):
            properties = schema.get("properties")
            allowed = set(properties) if isinstance(properties, Mapping) else set()
        else:
            allowed = set()
        # Test doubles and legacy metadata without a property projection do
        # not provide a contract to filter against; preserve their boundary
        # shape and let the injected handler validate it.
        if not allowed:
            return dict(arguments)
        return {key: value for key, value in arguments.items() if key in allowed}

    @staticmethod
    def _resource_id(
        resource_arg: str,
        target: TargetCandidate | None,
        session: Any,
    ) -> str:
        if target is not None:
            return str(
                target.resource_id
                or target.id
                or getattr(target, resource_arg, "")
                or ""
            )
        if session is not None:
            session_field = {
                "draft_id": "active_draft_id",
                "schedule_id": "active_schedule_id",
                "post_id": "active_post_id",
            }.get(resource_arg)
            if session_field:
                return str(getattr(session, session_field, "") or "")
        return ""

    # ── result mapping ───────────────────────────────────────────────

    def _read_result(self, request: TurnRequest, tool: Any, value: Any) -> RuntimeResult:
        payload: Mapping[str, Any] | None
        if isinstance(value, Mapping):
            payload = value
        else:
            # The production TurnCoordinator read boundary returns the
            # canonical RuntimeResult envelope.  Accept its structured first
            # artifact without inventing a second read contract.
            artifacts = getattr(value, "artifacts", None) or ()
            payload = artifacts[0] if artifacts and isinstance(artifacts[0], Mapping) else None
            if payload is None and value is not None:
                payload = {
                    "ok": bool(getattr(value, "success", False)),
                    "status": str(getattr(value, "status", "") or ""),
                    "content": str(getattr(value, "content", "") or ""),
                }
        if payload is None:
            return self._fail(request, "FAST_READ_INVALID_RESULT")
        if not payload.get("ok", True):
            return self._fail(
                request,
                str(payload.get("code") or "FAST_READ_FAILED"),
                str(payload.get("error") or payload.get("message") or ""),
            )
        partial_results = dict(getattr(value, "partial_results", {}) or {})
        content = str(
            getattr(value, "content", "")
            or payload.get("content")
            or _stringify(payload)
        )
        return RuntimeResult(
            success=True,
            status="COMPLETED",
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_path="fast_path",
            content=content,
            summary=str(getattr(value, "summary", "") or content),
            artifacts=[dict(payload)],
            partial_results=partial_results,
        )

    def _submission_result(self, value: Any, request: TurnRequest) -> RuntimeResult:
        if isinstance(value, RuntimeResult):
            if not value.execution_path:
                value.execution_path = "fast_path"
            return value
        if not isinstance(value, Mapping):
            return self._fail(request, "FAST_WRITE_INVALID_SUBMISSION")
        if not value.get("ok", True):
            return self._fail(
                request,
                str(value.get("code") or "FAST_WRITE_FAILED"),
                str(value.get("error") or value.get("message") or ""),
            )
        return RuntimeResult(
            success=True,
            status=str(value.get("status") or "COMPLETED"),
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_id=value.get("execution_id"),
            execution_path="fast_path",
            content=str(value.get("user_message") or value.get("message") or ""),
            summary=str(value.get("message") or ""),
        )

    def _read_payload(self, tool: Any, arguments: Mapping[str, Any], value: Any) -> dict[str, Any]:
        payload = dict(arguments)
        if isinstance(value, Mapping):
            for key in ("status", "resource_id", "draft_id", "schedule_id", "post_id"):
                if value.get(key):
                    payload.setdefault(key, value[key])
        payload.setdefault("tool", getattr(tool, "name", ""))
        return payload

    @staticmethod
    def _fail(
        request: TurnRequest,
        code: str,
        message: str = "",
    ) -> RuntimeResult:
        return RuntimeResult(
            success=False,
            status="FAILED",
            run_id=request.run_id,
            trace_id=request.trace_id,
            execution_path="fast_path",
            error_code=code,
            error_message=message or code,
        )

    async def _emit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        context: AssembledTurnContext,
        task_id: str,
        activity_callback: Any = None,
    ) -> None:
        callback = activity_callback or self._activity_callback
        if callback is None:
            return
        owned = dict(payload)
        owned["task_id"] = task_id
        emitted = callback(event_type, owned)
        if inspect.isawaitable(emitted):
            await emitted


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


__all__ = ["FastPathExecutor"]
