"""GreenBook MCP-compatible in-process tool runtime."""

from __future__ import annotations

import logging
from inspect import Signature, signature
from typing import Any

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.observability.run_metrics import run_scope
from greenbook_contracts.identity import AuthContext
from greenbook_java_client.client import JavaClient, agent_run_scope
from pydantic import ValidationError

from . import tool_registry
from .context import ToolContext
from .tool_schemas import openai_parameters

logger = logging.getLogger(__name__)


class GreenBookMCPServer:
    """Tool runtime that dispatches registered handlers with validation."""

    def __init__(
        self,
        java: JavaClient,
        *,
        capability_registry: CapabilityRegistry | None = None,
        llm: Any = None,
        model: str = "",
    ) -> None:
        self.java = java
        self.llm = llm
        self.model = model
        tool_registry.validate_registered_tool_contracts(
            capability_registry=capability_registry,
        )

    async def execute_tool(
        self,
        tool_name: str,
        *,
        auth: AuthContext,
        session: SessionContext,
        trace_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
        approval_granted: bool = False,
        **kwargs: object,
    ) -> dict:
        """Execute a named MCP tool with injected context.

        The tool handler receives ToolContext and keyword args.
        User identity fields are injected, never from kwargs.
        """
        try:
            definition = tool_registry.get_tool(tool_name)
        except ValueError:
            return {
                "ok": False,
                "code": "VALIDATION_ERROR",
                "message": f"Unknown tool: {tool_name}",
                "user_message": f"Tool '{tool_name}' is not available.",
            }

        if definition.policy.requires_approval and not approval_granted:
            return {
                "ok": False,
                "code": "APPROVAL_REQUIRED",
                "message": f"{tool_name} requires explicit user approval",
                "user_message": "This action changes or deletes business data and needs your confirmation.",
                "retryable": False,
                "request_sent": False,
                "state": {
                    "phase": "PRE_EXECUTION_POLICY_BLOCKED",
                    "downstream_called": False,
                    "side_effect_started": False,
                    "safe_to_retry": True,
                },
                "trace_id": trace_id,
            }

        definition_model = definition.input_schema
        normalized_kwargs: dict[str, object]
        if definition_model is None:
            normalized_kwargs = dict(kwargs)
        else:
            try:
                arguments = definition_model.model_validate(kwargs)
            except ValidationError as exc:
                error_types = {str(item.get("type", "")) for item in exc.errors()}
                error_messages = {
                    str(item.get("msg", "")) for item in exc.errors()
                }
                time_alias_conflict = any(
                    "run_at and publish_at conflict" in message
                    for message in error_messages
                )
                code = (
                    "INVALID_TOOL_ARGUMENT"
                    if "extra_forbidden" in error_types or time_alias_conflict
                    else "TOOL_ARGUMENT_VALIDATION_FAILED"
                )
                missing_required_reference = ""
                for item in exc.errors():
                    location = item.get("loc") or ()
                    field = str(location[-1]) if location else ""
                    if item.get("type") in {"missing", "value_error.missing"}:
                        missing_required_reference = field
                        break
                logger.warning(
                    "tool_argument_validation_failed tool=%s code=%s fields=%s args=%s",
                    tool_name,
                    code,
                    sorted(
                        str(item.get("loc", ()))
                        for item in exc.errors()
                    ),
                    {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v) for k, v in dict(kwargs).items()},
                )
                user_message = (
                    "时间参数冲突：run_at 与 publish_at 的值不一致，本次尚未修改定时任务，可以安全重试。"
                    if time_alias_conflict
                    else "工具参数校验失败，本次尚未执行任何修改，请重试。"
                )
                return {
                    "ok": False,
                    "code": code,
                    "message": "Tool arguments failed schema validation",
                    "user_message": user_message,
                    # Schema/argument failures are deterministic validation
                    # errors.  They never enter the transport retry path.
                    "retryable": False,
                    "request_sent": False,
                    "missing_required_reference": missing_required_reference,
                    "state": {
                        "phase": "PRE_EXECUTION_VALIDATION_FAILED",
                        "downstream_called": False,
                        "side_effect_started": False,
                        "safe_to_retry": True,
                        "missing_required_reference": missing_required_reference,
                    },
                    "trace_id": trace_id,
                }
            normalized_kwargs = arguments.model_dump(
                mode="python",
                by_alias=False,
                exclude_none=True,
            )

        handler_signature: Signature = signature(definition.handler)
        tool_context = ToolContext(
            auth=auth,
            session=session,
            java=self.java,
            trace_id=trace_id,
            conversation_id=session.conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            approval_granted=approval_granted,
            llm=self.llm,
            model=self.model,
        )
        try:
            handler_signature.bind(
                tool_context,
                **normalized_kwargs,
            )
        except TypeError:
            logger.exception("tool_handler_signature_mismatch tool=%s", tool_name)
            return {
                "ok": False,
                "code": "PRE_EXECUTION_VALIDATION_FAILED",
                "message": "Tool handler signature does not match its schema",
                "user_message": "工具参数不匹配，本次尚未执行任何修改，请重试。",
                "retryable": False,
                "request_sent": False,
                "state": {
                    "phase": "PRE_EXECUTION_VALIDATION_FAILED",
                    "downstream_called": False,
                    "side_effect_started": False,
                    "safe_to_retry": True,
                },
                "trace_id": trace_id,
            }

        ctx = tool_context

        try:
            with agent_run_scope(agent_run_id), run_scope(agent_run_id):
                result = await definition.handler(ctx, **normalized_kwargs)
            raw_result = result.model_dump(mode="python") if hasattr(result, "model_dump") else result
            try:
                validated_result = definition.output_schema.model_validate(raw_result)
            except ValidationError:
                logger.exception("tool_output_schema_validation_failed tool=%s", tool_name)
                has_side_effect = definition.policy.side_effect.has_side_effect
                return {
                    "ok": False,
                    # A malformed result after a write does not prove that
                    # the write failed. The handler may already have sent a
                    # request, so preserve the uncertainty for reconciliation.
                    "code": "RESULT_UNKNOWN" if has_side_effect else "TOOL_OUTPUT_VALIDATION_FAILED",
                    "message": (
                        "Side-effect tool output failed validation; downstream result is unknown"
                        if has_side_effect
                        else "Tool output failed its declared schema"
                    ),
                    "user_message": (
                        "The operation may have been submitted, but its actual result could not be confirmed. "
                        "Please do not repeat it while the status is checked."
                        if has_side_effect
                        else "Tool output validation failed. Please try again later."
                    ),
                    "retryable": False,
                    "request_sent": None if has_side_effect else True,
                    "state": {
                        "phase": "POST_EXECUTION_VALIDATION_FAILED",
                        "downstream_called": True,
                        "side_effect_started": has_side_effect,
                        "safe_to_retry": False,
                    },
                    "trace_id": trace_id,
                }
            if definition.policy.side_effect.has_side_effect:
                # A handler-level failure can be certain that no downstream
                # write happened (for example, generation failed before the
                # Java call).  Only successful handler results need the
                # verified-postcondition gate below.  Converting every
                # ``ok=False`` result into RESULT_UNKNOWN incorrectly pauses
                # deterministic pre-write failures and leaves the durable
                # execution waiting for reconciliation that has nothing to
                # reconcile.
                if not bool(getattr(validated_result, "ok", False)):
                    return validated_result.model_dump(mode="json")
                # This is the final truthful-completion gate.  Individual
                # write handlers must prove their Java postcondition with an
                # OperationReceipt; a plain ToolResult.success only proves
                # that Python received some return value.  Keeping this at
                # the registry boundary protects future write tools as well
                # as the current draft/schedule/reply handlers.
                receipt = getattr(validated_result, "operation_receipt", None)
                is_verified = bool(
                    receipt is not None
                    and receipt.result_known
                    and str(receipt.status or "").upper() == "COMPLETED"
                    and receipt.verification_evidence
                )
                if not is_verified:
                    logger.error(
                        "unverified_side_effect_success tool=%s receipt_present=%s",
                        tool_name,
                        receipt is not None,
                    )
                    receipt_payload = (
                        receipt.model_dump(mode="json") if receipt is not None else None
                    )
                    if receipt_payload is not None:
                        receipt_payload["result_known"] = False
                        receipt_payload["status"] = "RESULT_UNKNOWN"
                    return {
                        "ok": False,
                        "code": "RESULT_UNKNOWN",
                        "message": (
                            "Side-effect tool returned without verified business postcondition"
                        ),
                        "user_message": (
                            "The operation may have been submitted, but its actual result could not be confirmed. "
                            "Please do not repeat it while the status is checked."
                        ),
                        "retryable": False,
                        "request_sent": None,
                        "state": {
                            "phase": "POSTCONDITION_EVIDENCE_MISSING",
                            "downstream_called": True,
                            "side_effect_started": True,
                            "safe_to_retry": False,
                        },
                        "trace_id": trace_id,
                        "receipt_id": getattr(validated_result, "receipt_id", None),
                        "resource_refs": [
                            item.model_dump(mode="json")
                            for item in (getattr(validated_result, "resource_refs", None) or [])
                        ],
                        "operation_receipt": receipt_payload,
                    }
            return validated_result.model_dump(mode="json")
        except Exception:
            logger.exception("Tool '%s' execution failed", tool_name)
            has_side_effect = definition.policy.side_effect.has_side_effect
            return {
                "ok": False,
                # There is no generic way to prove an arbitrary handler
                # failed before its first downstream call. Safety wins for a
                # write: reconcile instead of falsely claiming failure.
                "code": "RESULT_UNKNOWN" if has_side_effect else "INTERNAL_ERROR",
                "message": (
                    "Side-effect tool raised an exception; downstream result is unknown"
                    if has_side_effect
                    else "Tool execution failed"
                ),
                "user_message": (
                    "The operation may have been submitted, but its actual result could not be confirmed. "
                    "Please do not repeat it while the status is checked."
                    if has_side_effect
                    else "Tool execution failed. Please try again later."
                ),
                "retryable": False,
                "request_sent": None if has_side_effect else False,
                "state": {
                    "phase": "TOOL_HANDLER_EXCEPTION",
                    "downstream_called": None if has_side_effect else False,
                    "side_effect_started": has_side_effect,
                    "safe_to_retry": False if has_side_effect else True,
                },
                "trace_id": trace_id,
            }

    def get_tool_definitions(self) -> list[dict]:
        """Export tool definitions for LLM function-calling."""
        tools = []
        for td in tool_registry.list_tools():
            item: dict[str, object] = {
                "name": td.name,
                "description": td.description,
                "category": td.category,
                "risk": td.policy.risk_level,
            }
            item["capability"] = td.capability
            item["operations"] = list(td.operations)
            item["semantic_action"] = (
                td.semantic_action.value if td.semantic_action is not None else None
            )
            item["parameters"] = openai_parameters(td.input_schema)
            item["output_schema"] = td.output_schema.model_json_schema()
            item["permission"] = td.policy.permission.model_dump(mode="json")
            item["retry_policy"] = td.policy.retry_policy.model_dump(mode="json")
            item["side_effect"] = td.policy.side_effect.model_dump(mode="json")
            tools.append(item)
        return tools
