import asyncio
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from agents.moderation.nodes.tool_agent_model import (
    LLMModerationToolAgent,
    ToolAgentInvocationError,
)
from agents.moderation.state import ModerationState
from agents.moderation.tools import build_moderation_tools


class FakeBindableModel:
    def __init__(self, response: AIMessage, *, delay: float = 0.0) -> None:
        self.response = response
        self.delay = delay
        self.bound_tools = None
        self.messages = None
        self.config = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages, config):
        self.messages = messages
        self.config = config
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.response


def _state() -> ModerationState:
    task_id = str(uuid4())
    return {
        "task_id": task_id,
        "thread_id": task_id,
        "content": "他的手机号是 13812345678",
        "normalized_content": "他的手机号是 13812345678",
        "content_hash": "content-hash",
        "content_type": "TEXT",
        "platform": "community",
        "classification": {
            "risk_type": "PRIVACY",
            "risk_score": 0.9,
            "confidence": 0.95,
            "indicators": ["phone number"],
        },
        "tool_call_round": 0,
        "tool_call_count": 0,
        "failed_tools": [],
    }


@pytest.mark.asyncio
async def test_model_binds_tools_without_forced_response_format_and_sanitizes_message(
    monkeypatch,
) -> None:
    raw = AIMessage(
        content="private analysis",
        tool_calls=[
            {
                "name": "detect_contact_information",
                "args": {"content": "他的手机号是 13812345678"},
                "id": "call-contact",
            }
        ],
        additional_kwargs={"reasoning_content": "do not persist"},
        response_metadata={"raw_private": "13812345678"},
        usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
    )
    model = FakeBindableModel(raw)
    monkeypatch.setattr(
        "agents.moderation.nodes.tool_agent_model.get_model",
        lambda _name: model,
    )
    tools = build_moderation_tools()

    result = await LLMModerationToolAgent(timeout_seconds=1).invoke(
        messages=[],
        tools=tools,
        state=_state(),
        config=RunnableConfig(configurable={"model": "fake"}),
    )

    assert model.bound_tools is tools
    assert result.message.content == ""
    assert result.message.additional_kwargs == {}
    assert result.message.response_metadata == {}
    assert result.message.tool_calls[0]["name"] == "detect_contact_information"
    assert result.metrics.total_tokens == 25
    assert model.config["run_name"] == "moderation_tool_agent"
    assert "13812345678" not in str(model.config["metadata"])
    assert "normalized_content" not in model.config["metadata"]


@pytest.mark.asyncio
async def test_model_preserves_final_json_but_drops_provider_metadata(monkeypatch) -> None:
    final_json = '{"complete":true,"recommended_path":"FAST_REVIEW"}'
    model = FakeBindableModel(
        AIMessage(
            content=final_json,
            additional_kwargs={"reasoning_content": "hidden"},
            response_metadata={"provider": "private"},
        )
    )
    monkeypatch.setattr(
        "agents.moderation.nodes.tool_agent_model.get_model",
        lambda _name: model,
    )

    result = await LLMModerationToolAgent(timeout_seconds=1).invoke(
        messages=[],
        tools=build_moderation_tools(),
        state=_state(),
        config=RunnableConfig(configurable={"model": "fake"}),
    )

    assert result.message.content == final_json
    assert result.message.additional_kwargs == {}
    assert result.message.response_metadata == {}


@pytest.mark.asyncio
async def test_model_timeout_is_wrapped_with_safe_metrics(monkeypatch) -> None:
    model = FakeBindableModel(AIMessage(content="{}"), delay=0.05)
    monkeypatch.setattr(
        "agents.moderation.nodes.tool_agent_model.get_model",
        lambda _name: model,
    )

    with pytest.raises(ToolAgentInvocationError) as exc_info:
        await LLMModerationToolAgent(timeout_seconds=0.01).invoke(
            messages=[],
            tools=build_moderation_tools(),
            state=_state(),
            config=RunnableConfig(configurable={"model": "fake"}),
        )

    assert exc_info.value.code == "moderation_tool_agent:TimeoutError"
    assert exc_info.value.metrics.model_name == "fake"
