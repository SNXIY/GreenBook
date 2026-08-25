"""Grounded community answer contract tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_java_client.models import EvidenceChunk, KnowledgeEvidenceResponse
from greenbook_mcp_server.context import ToolContext
from greenbook_mcp_server.tools import community


class _FakeJava:
    def __init__(self, response: ToolResult[KnowledgeEvidenceResponse]) -> None:
        self.response = response
        self.calls = 0

    async def retrieve_knowledge_evidence(self, *_: Any, **__: Any) -> ToolResult[KnowledgeEvidenceResponse]:
        self.calls += 1
        return self.response


class _FakeLLM:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.calls = 0
        self.payload = payload
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **_: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(self.payload, ensure_ascii=False),
            ))],
        )


def _evidence() -> KnowledgeEvidenceResponse:
    return KnowledgeEvidenceResponse(
        chunks=[EvidenceChunk(
            chunkId="chunk-1",
            postId="post-1",
            title="Java reliability",
            content="Use timeouts and circuit breakers.",
            score=0.91,
            startOffset=12,
            endOffset=48,
            eventVersion=3,
            updatedAt=datetime.now(UTC),
        )],
        candidatePostCount=1,
        embeddingLatencyMs=4,
        chunkRetrievalLatencyMs=9,
        degraded=False,
    )


def _ctx(java: Any, llm: Any = None) -> ToolContext:
    return ToolContext(
        auth=AuthContext(
            user_id="u1",
            tenant_id="t1",
            roles=[],
            timezone="Asia/Shanghai",
            raw_access_token="token",
        ),
        session=SessionContext(conversation_id="c1", user_id="u1", tenant_id="t1"),
        java=java,
        llm=llm,
        model="fake-model",
        trace_id="trace-1",
    )


@pytest.mark.asyncio
async def test_no_evidence_returns_exact_insufficient_answer_without_llm() -> None:
    java = _FakeJava(ToolResult.success(KnowledgeEvidenceResponse()))
    result = await community.answer_from_knowledge(_ctx(java), "unknown question")

    assert result.ok is True
    assert result.data == {"answer": "当前社区资料不足", "sources": []}
    assert java.calls == 1


@pytest.mark.asyncio
async def test_grounded_answer_rewrites_sources_from_canonical_evidence() -> None:
    java = _FakeJava(ToolResult.success(_evidence(), trace_id="trace-1"))
    llm = _FakeLLM({
        "answer": "Use timeouts and circuit breakers.",
        "sources": [{"postId": "post-1", "title": "invented title", "chunkId": "chunk-1"}],
    })

    result = await community.answer_from_knowledge(_ctx(java, llm), "How do I improve reliability?")

    assert result.ok is True
    assert result.data == {
        "answer": "Use timeouts and circuit breakers.",
        "sources": [{"postId": "post-1", "title": "Java reliability", "chunkId": "chunk-1"}],
    }
    assert result.resource_refs[0].kind == "POST_CHUNK"
    assert result.resource_refs[0].resource_id == "chunk-1"
    assert llm.calls == 1


@pytest.mark.asyncio
async def test_unknown_citation_fails_closed_to_insufficient_answer() -> None:
    java = _FakeJava(ToolResult.success(_evidence()))
    llm = _FakeLLM({
        "answer": "This is an unsupported answer.",
        "sources": [{"postId": "post-999", "title": "made up", "chunkId": "chunk-999"}],
    })

    result = await community.answer_from_knowledge(_ctx(java, llm), "question")

    assert result.ok is True
    assert result.data == {"answer": "当前社区资料不足", "sources": []}


@pytest.mark.asyncio
async def test_generation_without_host_llm_is_retryable_and_does_not_guess() -> None:
    java = _FakeJava(ToolResult.success(_evidence()))
    result = await community.answer_from_knowledge(_ctx(java), "question")

    assert result.ok is False
    assert result.code == "GENERATION_UNAVAILABLE"
    assert result.retryable is True
