"""Assistant-first lightweight draft generation: create_draft with an injected
LLM must generate the body directly (one call, no Creator pipeline) and save
through Java. The standalone Creator Service is retired."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from greenbook_agent_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_java_client.models import DraftResponse
from greenbook_mcp_server.context import ToolContext
from greenbook_mcp_server.tools import content
from greenbook_agent_core.observability.run_metrics import run_scope, snapshot


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "title": "直接生成的标题",
                "body_markdown": "这是一篇直接生成的正文。\n\n## 第一节\n内容…",
            }, ensure_ascii=False)))]
        )


class _FakeJava:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.draft: DraftResponse | None = None

    async def create_draft(self, request: Any, **_: Any) -> Any:
        self.created.append(request)
        self.draft = DraftResponse(
            draftId="draft-1",
            title=request.title,
            content=request.content,
            summary=request.summary,
            status="draft",
            version=1,
            updatedAt=datetime.now(UTC),
        )
        return SimpleNamespace(
            ok=True,
            data=self.draft,
            receipt_id="receipt-1",
        )

    async def get_draft(self, draft_id: str, **_: Any) -> Any:
        assert draft_id == "draft-1"
        return SimpleNamespace(ok=True, data=self.draft)


def _ctx(llm: Any, java: Any) -> ToolContext:
    return ToolContext(
        auth=AuthContext(user_id="u1", tenant_id="t1", roles=[], timezone="Asia/Shanghai", raw_access_token="t"),
        session=SessionContext(conversation_id="c1", user_id="u1", tenant_id="t1"),
        java=java,  # type: ignore[arg-type]
        llm=llm,
        model="fake-model",
    )


@pytest.mark.asyncio
async def test_create_draft_generates_directly_without_creator() -> None:
    llm = _FakeLLM()
    java = _FakeJava()
    ctx = _ctx(llm, java)

    result = await content.create_draft(
        ctx,
        title="如何学习 Agent",
        instruction="写一篇如何学习 agent 的帖子，参考社区讨论",
        references=[{"post_id": "p1", "title": "Agent 入门", "summary": "要点"}],
    )

    assert result.ok is True
    data = result.data or {}
    assert data["draft_id"] == "draft-1"
    assert data["generation"] == "assistant_direct"
    assert llm.calls == 1  # exactly one LLM round trip
    assert len(java.created) == 1
    assert "直接生成的正文" in java.created[0].content
    assert ctx.session.active_draft_id == "draft-1"


@pytest.mark.asyncio
async def test_creator_llm_is_attributed_to_the_active_agent_run() -> None:
    llm = _FakeLLM()
    ctx = _ctx(llm, _FakeJava())

    with run_scope("run-creator-metrics"):
        result = await content.create_draft(ctx, title="Agent", instruction="write a draft")

    metrics = snapshot("run-creator-metrics")
    assert result.ok is True
    assert metrics["llm_calls"] == 1
    assert metrics["creator_llm_calls"] == 1
    assert metrics["creator_latency_ms"] >= 0


@pytest.mark.asyncio
async def test_create_draft_rejects_unknown_strategy_arguments() -> None:
    """strategy arguments no longer exist on the lightweight tool."""
    llm = _FakeLLM()
    java = _FakeJava()
    ctx = _ctx(llm, java)

    with pytest.raises(TypeError):
        await content.create_draft(
            ctx,
            title="策略文章",
            instruction="基于策略写文章",
            strategy_task_id="st-1",
            strategy_artifact_id="sa-1",
        )


@pytest.mark.asyncio
async def test_direct_generation_failure_returns_error_not_crash() -> None:
    class _BrokenLLM:
        def __init__(self) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._fail))

        async def _fail(self, **_: Any) -> Any:
            raise RuntimeError("provider down")

    java = _FakeJava()
    ctx = _ctx(_BrokenLLM(), java)
    result = await content.create_draft(ctx, title="t", instruction="i")
    assert result.ok is False  # clean failure
