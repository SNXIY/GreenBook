from __future__ import annotations

import math

import pytest

from app.memory import (
    HashingMemoryEmbedder,
    _artifact_refs,
    bound_recalled_memories,
    is_sensitive_memory,
)


@pytest.mark.asyncio
async def test_hashing_memory_embedder_preserves_lexical_similarity() -> None:
    embedder = HashingMemoryEmbedder(256)
    related, paraphrase, unrelated = await embedder.embed(
        (
            "学习 MySQL 数据库索引与查询优化",
            "MySQL 索引学习和查询优化方法",
            "明天北京天气和旅行路线",
        )
    )

    related_score = sum(a * b for a, b in zip(related, paraphrase))
    unrelated_score = sum(a * b for a, b in zip(related, unrelated))

    assert math.isclose(sum(value * value for value in related), 1.0)
    assert related_score > unrelated_score


@pytest.mark.parametrize(
    "value",
    [
        "请记住我的密码是 abc123",
        "API_KEY=secret-value",
        "Authorization: Bearer abc.def.ghi",
        "身份证号码是 110101199001011234",
    ],
)
def test_sensitive_requests_are_not_automatically_memorized(value: str) -> None:
    assert is_sensitive_memory(value)


def test_normal_task_can_be_consolidated() -> None:
    assert not is_sensitive_memory("帮我分析社区热点并生成一篇 Java 学习帖子")


def test_recalled_memory_context_has_a_deterministic_budget() -> None:
    bounded = bound_recalled_memories(
        [
            {
                "memory_id": str(index),
                "kind": "TASK_KNOWLEDGE",
                "content": "数据库索引优化" * 300,
            }
            for index in range(10)
        ],
        max_chars=2_000,
    )

    assert bounded
    assert len(bounded) < 10
    assert len(str(bounded)) <= 2_200


def test_artifact_references_are_deduplicated() -> None:
    refs = _artifact_refs(
        [
            {"result": {"draft_id": "draft-1", "post_id": "post-1"}},
            {"result": {"draft_id": "draft-1", "creator_task_id": "task-1"}},
        ]
    )

    assert refs == [
        {"type": "draft_id", "id": "draft-1"},
        {"type": "post_id", "id": "post-1"},
        {"type": "creator_task_id", "id": "task-1"},
    ]
