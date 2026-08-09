import json

import pytest

from app.domain import CommunityIntent
from app.llm import DeepSeekClient, _extract_json, _parse_json_model


def intent_payload() -> dict:
    return {
        "domain": "general_answer",
        "goal": "回答今天的日期",
        "priority": "normal",
        "constraints": [],
        "required_capabilities": [],
        "entities": {},
        "scope": {},
        "risk": "low",
        "confidence": 0.99,
    }


def test_extract_json_uses_first_complete_object_not_first_to_last_brace() -> None:
    raw = '{"first":1}\n{"second":2}'
    assert json.loads(_extract_json(raw)) == {"first": 1}


def test_parse_model_accepts_fenced_json_with_surrounding_text() -> None:
    raw = (
        "下面是结果：\n```json\n"
        + json.dumps(intent_payload(), ensure_ascii=False)
        + "\n```\n请查收"
    )
    parsed = _parse_json_model(raw, CommunityIntent)
    assert parsed.domain == "general_answer"


def test_parse_model_skips_an_invalid_object_before_valid_contract() -> None:
    raw = (
        '{"example":"not the response"}\n'
        + json.dumps(intent_payload(), ensure_ascii=False)
    )
    parsed = _parse_json_model(raw, CommunityIntent)
    assert parsed.goal == "回答今天的日期"


def test_parse_model_rejects_text_without_json_object() -> None:
    with pytest.raises(ValueError, match="没有返回 JSON"):
        _parse_json_model("今天是七月二十九日", CommunityIntent)


@pytest.mark.asyncio
async def test_structured_chat_repairs_invalid_first_response() -> None:
    client = object.__new__(DeepSeekClient)
    responses = iter(
        [
            '{"domain":"content_publish"}',
            json.dumps(intent_payload(), ensure_ascii=False),
        ]
    )

    async def fake_chat(
        messages,
        *,
        temperature,
        json_mode=False,
        operation,
        force_repair=False,
    ):
        del messages, temperature, force_repair
        assert json_mode is True
        assert operation in {"intent.understand", "structured.repair"}
        return next(responses)

    client._chat = fake_chat
    result = await client._structured_chat(
        [{"role": "system", "content": "return JSON"}],
        model_type=CommunityIntent,
        temperature=0.0,
        operation="intent.understand",
    )
    assert result.goal == "回答今天的日期"


@pytest.mark.asyncio
async def test_structured_chat_fails_after_one_bounded_repair() -> None:
    client = object.__new__(DeepSeekClient)
    call_count = 0

    async def fake_chat(
        messages,
        *,
        temperature,
        json_mode=False,
        operation,
        force_repair=False,
    ):
        nonlocal call_count
        del messages, temperature, json_mode, operation, force_repair
        call_count += 1
        return '{"invalid":true}'

    client._chat = fake_chat
    with pytest.raises(ValueError, match="连续两次"):
        await client._structured_chat(
            [{"role": "system", "content": "return JSON"}],
            model_type=CommunityIntent,
            temperature=0.0,
            operation="intent.understand",
        )
    assert call_count == 2
