"""Fast-track acceptance tests for the canonical Command Runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from greenbook_agent_core.command import (
    CommandContext,
    CommandInterpreter,
    CommandType,
    TargetKind,
)


class _FakeCompletions:
    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = iter(payloads)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload = next(self._payloads)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=str_json(payload)))]
        )


class _FakeLLM:
    def __init__(self, payloads: list[dict]) -> None:
        self.completions = _FakeCompletions(payloads)
        self.chat = SimpleNamespace(completions=self.completions)


class _JsonSchemaFallbackCompletions:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            raise RuntimeError("400 invalid_request_error: response_format type is unavailable")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=str_json(self.payload)))],
        )


class _JsonSchemaFallbackLLM:
    def __init__(self, payload: dict) -> None:
        self.completions = _JsonSchemaFallbackCompletions(payload)
        self.chat = SimpleNamespace(completions=self.completions)


def str_json(value: dict) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


@pytest.mark.asyncio
async def test_create_java_article_returns_create_command() -> None:
    llm = _FakeLLM([
        {
            "command": "CREATE",
            "objective": "创建一篇Java文章",
            "target": None,
            "parameters": {"topic": "Java"},
            "confidence": 0.98,
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "创建一篇Java文章"
    )

    assert command.type == CommandType.CREATE
    assert command.command == CommandType.CREATE
    assert llm.completions.calls[0]["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_unsupported_json_schema_uses_json_object_fallback() -> None:
    llm = _JsonSchemaFallbackLLM(
        {
            "command": "CREATE",
            "objective": "总结时间管理帖子",
            "target": None,
            "parameters": {},
            "confidence": 0.9,
        },
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "帮我总结时间管理帖子",
    )

    assert command.type == CommandType.CREATE
    assert [call["response_format"]["type"] for call in llm.completions.calls] == [
        "json_schema",
        "json_object",
    ]
    assert '"entities"' in llm.completions.calls[1]["messages"][0]["content"]
    assert '"required_capabilities"' in llm.completions.calls[1]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_modify_previous_target_is_resolved_from_active_context() -> None:
    llm = _FakeLLM([
        {
            "command": "MODIFY",
            "objective": "调整发布时间",
            "target": {
                "kind": "SCHEDULE",
                "reference_type": "ACTIVE",
            },
            "parameters": {"run_at": "22:00"},
            "confidence": 0.94,
        }
    ])
    context = CommandContext(
        active_target={"kind": "SCHEDULE", "id": "schedule-java"},
        targets=[{"kind": "SCHEDULE", "id": "schedule-java", "status": "SCHEDULED"}],
    )

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "把刚才那个改成晚上10点",
        context,
    )

    assert command.type == CommandType.MODIFY
    assert command.target is not None
    assert command.target.id == "schedule-java"
    assert command.target_resolution == "RESOLVED"


@pytest.mark.asyncio
async def test_cancel_yesterday_schedule_returns_cancel_command() -> None:
    llm = _FakeLLM([
        {
            "command": "CANCEL",
            "objective": "取消昨天安排的发布任务",
            "target": {
                "kind": "SCHEDULE",
                "id": "schedule-yesterday",
                "reference_type": "IDENTIFIER",
            },
            "parameters": {},
            "confidence": 0.96,
        }
    ])

    command = await CommandInterpreter(llm=llm, model="test").interpret(
        "取消昨天安排的发布任务",
        CommandContext(
            targets=[
                {
                    "kind": TargetKind.SCHEDULE,
                    "id": "schedule-yesterday",
                    "status": "SCHEDULED",
                }
            ]
        ),
    )

    assert command.type == CommandType.CANCEL
    assert command.target_exists is True
    assert command.target_resolution == "RESOLVED"
