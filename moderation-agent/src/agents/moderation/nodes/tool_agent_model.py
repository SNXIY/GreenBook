import asyncio
from time import perf_counter
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from agents.moderation.nodes.dependencies import ToolAgentCall
from agents.moderation.state import ModerationState
from core import get_model, settings
from moderation.schemas import ToolAgentMetrics


class ToolAgentInvocationError(RuntimeError):
    def __init__(self, code: str, metrics: ToolAgentMetrics) -> None:
        super().__init__(code)
        self.code = code
        self.metrics = metrics


class LLMModerationToolAgent:
    def __init__(self, timeout_seconds: float | None = None) -> None:
        self.timeout_seconds = (
            settings.MODERATION_TOOL_AGENT_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

    async def invoke(
        self,
        *,
        messages: list[AnyMessage],
        tools: list[BaseTool],
        state: ModerationState,
        config: RunnableConfig,
    ) -> ToolAgentCall:
        started = perf_counter()
        model_name_value = config.get("configurable", {}).get("model", settings.DEFAULT_MODEL)
        model_name = str(model_name_value or "unconfigured")
        try:
            model = get_model(model_name_value)  # type: ignore[arg-type]
            runnable = model.bind_tools(tools)
            call_config = _call_config(config, state, tools, model_name)
            async with asyncio.timeout(self.timeout_seconds):
                response = await runnable.ainvoke(messages, call_config)
            if not isinstance(response, AIMessage):
                raise TypeError("tool agent model did not return an AIMessage")
            sanitized = sanitize_tool_agent_message(response)
            return ToolAgentCall(
                message=sanitized,
                metrics=_metrics(model_name, started, response),
            )
        except Exception as exc:
            metrics = _metrics(model_name, started)
            code = f"moderation_tool_agent:{type(exc).__name__}"
            raise ToolAgentInvocationError(code, metrics) from exc


def sanitize_tool_agent_message(message: AIMessage) -> AIMessage:
    if message.invalid_tool_calls:
        raise ValueError("tool agent returned invalid tool calls")

    tool_calls = [
        {
            "name": call["name"],
            "args": dict(call["args"]),
            "id": call["id"],
            "type": "tool_call",
        }
        for call in message.tool_calls
    ]
    content = "" if tool_calls else _text_content(message.content)
    if not tool_calls and not content.strip():
        raise ValueError("tool agent returned neither tool calls nor a final result")
    return AIMessage(
        content=content,
        tool_calls=tool_calls,
        id=message.id,
        usage_metadata=message.usage_metadata,
    )


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    text_parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            text_parts.append(block)
        elif isinstance(block, dict) and block.get("type") in {"text", "text_delta"}:
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return "\n".join(text_parts)


def _call_config(
    config: RunnableConfig,
    state: ModerationState,
    tools: list[BaseTool],
    model_name: str,
) -> RunnableConfig:
    call_config = config.copy()
    call_config.pop("run_id", None)
    call_config["run_name"] = "moderation_tool_agent"
    call_config["tags"] = list(
        dict.fromkeys(
            [
                *config.get("tags", []),
                "moderation",
                "tool_calling",
                "moderation_tool_agent",
                "skip_stream",
            ]
        )
    )
    classification = state.get("classification", {})
    call_config["metadata"] = {
        "moderation_task_id": state.get("task_id"),
        "initial_risk_type": classification.get("risk_type"),
        "model_name": model_name,
        "available_tools": [tool.name for tool in tools],
        "tool_call_round": state.get("tool_call_round", 0) + 1,
        "total_tool_calls": state.get("tool_call_count", 0),
        "failed_tools": list(state.get("failed_tools", [])),
    }
    return call_config


def _metrics(
    model_name: str,
    started: float,
    message: AIMessage | None = None,
) -> ToolAgentMetrics:
    usage = message.usage_metadata if message is not None else None
    input_tokens = usage.get("input_tokens") if usage else None
    output_tokens = usage.get("output_tokens") if usage else None
    total_tokens = usage.get("total_tokens") if usage else None
    return ToolAgentMetrics(
        model_name=model_name,
        latency_ms=(perf_counter() - started) * 1000,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )
