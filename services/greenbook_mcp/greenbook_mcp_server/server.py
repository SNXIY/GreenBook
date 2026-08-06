"""GreenBook MCP Server — assembles tools and provides the execution boundary.

Phase 1: In-process tool abstraction (no remote MCP transport required).
Phase 2: Streamable HTTP MCP Server.
"""

from __future__ import annotations

import logging
from inspect import Signature, signature

from greenbook_assistant_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_creator_client.client import CreatorClient
from greenbook_java_client.client import JavaClient
from pydantic import ValidationError

from . import tool_registry
from .context import ToolContext
from .tool_schemas import openai_parameters

logger = logging.getLogger(__name__)


class GreenBookMCPServer:
    """In-process MCP server that dispatches tool calls with Pydantic validation."""

    def __init__(
        self,
        java: JavaClient,
        creator: CreatorClient,
    ) -> None:
        self.java = java
        self.creator = creator
        tool_registry.validate_registered_tool_contracts()

    async def execute_tool(
        self,
        tool_name: str,
        *,
        auth: AuthContext,
        session: SessionContext,
        trace_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
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

        definition_model = definition.argument_model
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
                logger.warning(
                    "tool_argument_validation_failed tool=%s code=%s fields=%s",
                    tool_name,
                    code,
                    sorted(
                        str(item.get("loc", ()))
                        for item in exc.errors()
                    ),
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
                    "retryable": True,
                    "request_sent": False,
                    "state": {
                        "phase": "PRE_EXECUTION_VALIDATION_FAILED",
                        "downstream_called": False,
                        "side_effect_started": False,
                        "safe_to_retry": True,
                    },
                    "trace_id": trace_id,
                }
            normalized_kwargs = arguments.model_dump(
                mode="python",
                by_alias=False,
                exclude_none=True,
            )

        handler_signature: Signature = signature(definition.handler)
        try:
            handler_signature.bind(
                ToolContext(
                    auth=auth,
                    session=session,
                    java=self.java,
                    creator=self.creator,
                    trace_id=trace_id,
                    conversation_id=session.conversation_id,
                    agent_run_id=agent_run_id,
                    tool_call_id=tool_call_id,
                ),
                **normalized_kwargs,
            )
        except TypeError:
            logger.exception("tool_handler_signature_mismatch tool=%s", tool_name)
            return {
                "ok": False,
                "code": "PRE_EXECUTION_VALIDATION_FAILED",
                "message": "Tool handler signature does not match its schema",
                "user_message": "工具参数不匹配，本次尚未执行任何修改，请重试。",
                "retryable": True,
                "request_sent": False,
                "state": {
                    "phase": "PRE_EXECUTION_VALIDATION_FAILED",
                    "downstream_called": False,
                    "side_effect_started": False,
                    "safe_to_retry": True,
                },
                "trace_id": trace_id,
            }

        ctx = ToolContext(
            auth=auth,
            session=session,
            java=self.java,
            creator=self.creator,
            trace_id=trace_id,
            conversation_id=session.conversation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
        )

        try:
            result = await definition.handler(ctx, **normalized_kwargs)
            if hasattr(result, "model_dump"):
                return result.model_dump(mode="json")
            return result
        except Exception:
            logger.exception("Tool '%s' execution failed", tool_name)
            return {
                "ok": False,
                "code": "TOOL_EXECUTION_FAILED",
                "message": "Tool execution failed",
                "user_message": "工具执行失败，请稍后重试。",
                "retryable": False,
                "request_sent": False,
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
                "risk": td.risk,
            }
            if td.argument_model is not None:
                item["parameters"] = openai_parameters(td.argument_model)
            tools.append(item)
        return tools
