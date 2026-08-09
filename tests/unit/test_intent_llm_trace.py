"""Stage E-2.5 tests for Direct IntentSpec LLM diagnostics."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from greenbook_assistant_core.task.understanding import TaskUnderstanding


class _TraceCompletions:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.index = 0
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents[self.index]
        self.index += 1
        response = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content=content),
                finish_reason="stop",
            )],
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )
        return response


class _TraceLLM:
    def __init__(self, contents: list[str]) -> None:
        self.chat = SimpleNamespace(completions=_TraceCompletions(contents))


def _valid_create() -> str:
    return json.dumps({
        "mode": "SIMPLE",
        "goal": "创建文章",
        "actions": [{"action": "CREATE", "resource": "CONTENT"}],
        "conditions": [],
        "constraints": [],
        "target_hint": None,
        "confidence": 0.9,
    })


@pytest.mark.asyncio
async def test_llm_trace_lifecycle() -> None:
    tu = TaskUnderstanding(llm=_TraceLLM(["", _valid_create()]), model="trace-model")

    result = await tu._llm_understand_direct_v2("创建一篇文章")

    assert result is not None
    assert len(tu.llm_traces) == 2
    first, second = tu.llm_traces
    assert first.raw_response_content == ""
    assert first.parse_status == "EMPTY_RESPONSE"
    assert second.parse_status == "PARSED"
    assert second.model == "trace-model"
    assert second.finish_reason == "stop"
    assert second.usage["completion_tokens"] == 5
    assert second.latency_ms >= 0


@pytest.mark.asyncio
async def test_empty_response_capture() -> None:
    tu = TaskUnderstanding(llm=_TraceLLM(["", ""]), model="trace-model")

    result = await tu._try_l2_v2("复杂运营任务")

    assert result is None
    assert len(tu.llm_traces) == 2
    assert all(trace.parse_status == "EMPTY_RESPONSE" for trace in tu.llm_traces)
    assert len(tu.validation_traces) == 1
    assert tu.validation_traces[0].validation_errors[0]["type"] == "EMPTY_LLM_RESPONSE"


@pytest.mark.asyncio
async def test_simple_request_uses_low_adaptive_budget() -> None:
    llm = _TraceLLM([_valid_create()])
    tu = TaskUnderstanding(llm=llm, model="trace-model")

    await tu._llm_understand_direct_v2("创建一篇文章")

    assert llm.chat.completions.calls[0]["max_tokens"] == 600


@pytest.mark.asyncio
async def test_complex_request_uses_high_adaptive_budget() -> None:
    llm = _TraceLLM([_valid_create()])
    tu = TaskUnderstanding(llm=llm, model="trace-model")

    await tu._llm_understand_direct_v2(
        "1. 搜索热门文章 2. 分析原因 3. 如果有旧稿就优化没有就创建 4. 发布前确认"
    )

    assert llm.chat.completions.calls[0]["max_tokens"] == 2000


@pytest.mark.asyncio
async def test_truncated_response_is_captured_in_validation_trace() -> None:
    llm = _TraceLLM(['{"mode":"SIMPLE"'])
    tu = TaskUnderstanding(llm=llm, model="trace-model")
    llm.chat.completions.create = _truncated_create(llm.chat.completions)

    result = await tu._try_l2_v2("创建一篇文章")

    assert result is None
    assert tu.llm_traces[0].parse_status == "TRUNCATED_RESPONSE"
    assert tu.validation_traces[0].validation_errors[0]["type"] == "TRUNCATED_RESPONSE"


def _truncated_create(completions):
    async def create(**kwargs):
        completions.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"mode":"SIMPLE"'),
                finish_reason="length",
            )],
            usage={"prompt_tokens": 10, "completion_tokens": 600},
        )
    return create
