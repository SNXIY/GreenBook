import json

from app.context_governance import (
    bounded_conversation,
    bounded_post,
    bounded_tool_outputs,
)
from app.tools import tool_registry


def _json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def test_conversation_uses_recent_suffix_without_duplicate_current_prompt() -> None:
    history = [
        {"role": "user", "content": "old-" + ("x" * 2_000)},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
        {"role": "user", "content": "current request"},
    ]

    result = bounded_conversation(
        history,
        current_prompt="current request",
        max_chars=500,
    )

    assert result[0]["role"] == "user"
    assert result[-1]["content"] == "recent answer"
    assert all(item["content"] != "current request" for item in result)
    assert sum(len(item["content"]) + len(item["role"]) + 16 for item in result) <= 500


def test_tool_outputs_are_bounded_without_mutating_durable_result() -> None:
    outputs = [
        {
            "ordinal": ordinal,
            "tool": "community.get_post",
            "label": f"读取帖子 {ordinal}",
            "result": {"body_markdown": "正文" * 8_000, "id": str(ordinal)},
        }
        for ordinal in range(1, 5)
    ]
    original_length = len(outputs[0]["result"]["body_markdown"])

    result = bounded_tool_outputs(outputs, max_chars=2_000)

    assert _json_size(result) <= 2_000
    assert result
    assert len(outputs[0]["result"]["body_markdown"]) == original_length


def test_post_context_and_tool_protocol_have_deterministic_boundaries() -> None:
    post = {
        "id": "42",
        "title": "Java 学习",
        "bodyMarkdown": "内容" * 20_000,
        "tags": ["Java", "后端"],
    }

    bounded = bounded_post(post, max_chars=4_000)

    assert _json_size(bounded) <= 4_000
    assert bounded["id"] == "42"
    assert len(tool_registry.signature()) == 64
    assert tool_registry.signature() == tool_registry.signature()
