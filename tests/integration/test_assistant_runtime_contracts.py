"""Regression tests for the live Assistant/MCP and thinking-model contracts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_core.agent import CommunityOperationsAssistant
from greenbook_assistant_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_creator_client.client import CreatorClient
from greenbook_java_client.client import JavaClient
from greenbook_mcp_server.context import ToolContext
from greenbook_mcp_server.tools.community import search_public_posts
from starlette.testclient import TestClient

from apps.assistant_api.greenbook_assistant_api.main import create_app


def _auth() -> AuthContext:
    return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="access-token")


def _session() -> SessionContext:
    return SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")


def test_tool_context_accepts_the_runtime_creator_and_java_clients() -> None:
    context = ToolContext(
        auth=_auth(),
        session=_session(),
        java=JavaClient(base_url="http://127.0.0.1:8080"),
        creator=CreatorClient(base_url="http://127.0.0.1:8092"),
    )
    assert context.creator is not None
    assert context.java.http.base_url == "http://127.0.0.1:8080"


@pytest.mark.asyncio
async def test_public_search_tool_relays_the_verified_access_token() -> None:
    java = SimpleNamespace(
        search_posts=AsyncMock(return_value=ToolResult.success({"posts": [], "total": 0}))
    )

    context = ToolContext(
        auth=_auth(),
        session=_session(),
        java=java,
        creator=CreatorClient(base_url="http://127.0.0.1:8092"),
        trace_id="trace-1",
        conversation_id="conv-1",
    )

    result = await search_public_posts(context, query="RAG")

    assert result.ok is True
    java.search_posts.assert_awaited_once_with(
        query="RAG",
        sort="latest",
        page=1,
        size=20,
        bearer_token="access-token",
        trace_id="trace-1",
        conversation_id="conv-1",
    )


def test_tool_context_idempotency_key_is_stable_across_retries() -> None:
    first = ToolContext(
        auth=_auth(),
        session=_session(),
        java=JavaClient(base_url="http://127.0.0.1:8080"),
        creator=CreatorClient(base_url="http://127.0.0.1:8092"),
        conversation_id="conv-1",
        agent_run_id="run-a",
        tool_call_id="call-a",
    )
    retry = ToolContext(
        auth=_auth(),
        session=_session(),
        java=JavaClient(base_url="http://127.0.0.1:8080"),
        creator=CreatorClient(base_url="http://127.0.0.1:8092"),
        conversation_id="conv-1",
        agent_run_id="run-b",
        tool_call_id="call-b",
    )

    assert first.idempotency_key("create_draft", "Java|guide") == retry.idempotency_key(
        "create_draft", "Java|guide"
    )
    assert first.idempotency_key("create_draft", "Java|other") != first.idempotency_key(
        "create_draft", "Java|guide"
    )


@pytest.mark.asyncio
async def test_creator_client_create_task_contract() -> None:
    client = CreatorClient(base_url="http://127.0.0.1:8092")
    response = SimpleNamespace(
        status_code=201,
        json=lambda: {"task_id": "task-1", "status": "QUEUED"},
    )
    client.http.post = AsyncMock(return_value=response)

    result = await client.create_task(
        kind="CREATE_CONTENT",
        goal="Java concurrency guide",
        bearer_token="access-token",
        idempotency_key="creator-key",
        trace_id="trace-1",
    )

    assert result.ok is True
    assert result.data == {"task_id": "task-1", "status": "QUEUED"}
    request = client.http.post.await_args
    assert request.args == ("/api/v1/creator/tasks",)
    assert request.kwargs["headers"] == {
        "Authorization": "Bearer access-token",
        "Idempotency-Key": "creator-key",
        "X-Trace-ID": "trace-1",
    }
    assert request.kwargs["json"]["kind"] == "CREATE_CONTENT"
    await client.close()


@pytest.mark.asyncio
async def test_thinking_tool_call_preserves_reasoning_for_the_second_request() -> None:
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="community_search_public_posts",
            arguments='{"query":"RAG"}',
        ),
    )
    first_message = SimpleNamespace(
        content=None,
        reasoning_content="internal reasoning that must not be user-visible",
        tool_calls=[tool_call],
    )
    second_message = SimpleNamespace(
        content="RAG 搜索完成。",
        reasoning_content=None,
        tool_calls=None,
    )
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(message=first_message)]),
            SimpleNamespace(choices=[SimpleNamespace(message=second_message)]),
        ]
    )
    llm = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    agent = CommunityOperationsAssistant(
        llm=llm,
        model="thinking-test-model",
        tools_schema=[{"type": "function", "function": {"name": "community_search_public_posts"}}],
    )

    async def tool_handler(
        name: str,
        args: dict[str, object],
        session: SessionContext,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, object]:
        assert name == "community_search_public_posts"
        assert args == {"query": "RAG"}
        return ToolResult.success({"posts": [{"post_id": "p-1"}]}).model_dump(mode="json")

    result = await agent.run(
        user_message="搜索社区里关于 RAG 的帖子",
        session=_session(),
        tool_handler=tool_handler,
        run_id="run-1",
    )

    second_messages = create.call_args_list[1].kwargs["messages"]
    assistant_messages = [m for m in second_messages if m.get("role") == "assistant"]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["reasoning_content"] == "internal reasoning that must not be user-visible"
    assert assistant_messages[0]["tool_calls"][0]["id"] == "call-1"
    assert any(
        m.get("role") == "tool" and m.get("tool_call_id") == "call-1"
        for m in second_messages
    )
    assert result["content"] == "RAG 搜索完成。"
    assert "reasoning_content" not in result


@pytest.mark.asyncio
async def test_cancelled_schedule_is_removed_from_active_session_binding() -> None:
    tool_call = SimpleNamespace(
        id="cancel-call",
        function=SimpleNamespace(
            name="publication_cancel_schedule",
            arguments="{}",
        ),
    )
    first_message = SimpleNamespace(
        content=None,
        reasoning_content="private reasoning",
        tool_calls=[tool_call],
    )
    second_message = SimpleNamespace(
        content="已取消定时任务。",
        reasoning_content=None,
        tool_calls=None,
    )
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(message=first_message)]),
            SimpleNamespace(choices=[SimpleNamespace(message=second_message)]),
        ]
    )
    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = CommunityOperationsAssistant(
        llm=llm,
        model="thinking-test-model",
        tools_schema=[
            {"type": "function", "function": {"name": "publication_cancel_schedule"}}
        ],
    )
    session = _session()
    session.active_schedule_id = "schedule-1"

    async def tool_handler(*args: object, **kwargs: object) -> dict[str, object]:
        return ToolResult.success(
            {"schedule_id": "schedule-1", "status": "CANCELLED"}
        ).model_dump(mode="json")

    result = await agent.run(
        user_message="取消刚才的发布任务",
        session=session,
        tool_handler=tool_handler,
    )

    assert result["content"] == "已取消定时任务。"
    assert session.active_schedule_id is None


def test_failed_tool_returns_http_error_and_failed_run_event() -> None:
    tool_call = SimpleNamespace(
        id="failed-call",
        function=SimpleNamespace(
            name="community_search_public_posts",
            arguments='{"query":"RAG"}',
        ),
    )
    first_message = SimpleNamespace(
        content=None,
        reasoning_content="must remain internal",
        tool_calls=[tool_call],
    )
    second_message = SimpleNamespace(
        content="工具执行失败。",
        reasoning_content=None,
        tool_calls=None,
    )
    llm = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    side_effect=[
                        SimpleNamespace(choices=[SimpleNamespace(message=first_message)]),
                        SimpleNamespace(choices=[SimpleNamespace(message=second_message)]),
                    ]
                )
            )
        )
    )

    class FailingMCP:
        async def execute_tool(self, *args: object, **kwargs: object) -> dict[str, object]:
            return ToolResult.dependency_unavailable(
                "Java is unavailable", trace_id="trace-failure"
            ).model_dump(mode="json")

    app = create_app(
        auth_validator=lambda token: AuthContext(
            user_id="u1", tenant_id="t1", raw_access_token=token
        )
    )
    app.state.conversation_store = {}
    app.state.run_store = {}
    app.state.approval_store = {}
    app.state.message_store = {}
    app.state.llm = llm
    app.state.mcp = FailingMCP()
    app.state.model = "thinking-test-model"

    client = TestClient(app)
    headers = {"Authorization": "Bearer test-access"}
    conversation = client.post(
        "/api/v1/assistant/conversations",
        json={"title": "failure"},
        headers=headers,
    )
    assert conversation.status_code == 200
    conversation_id = conversation.json()["conversation_id"]

    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        json={"content": "search community posts"},
        headers=headers,
    )
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "DEPENDENCY_UNAVAILABLE"
    run_id = detail["run_id"]

    run = client.get(f"/api/v1/assistant/runs/{run_id}", headers=headers)
    assert run.status_code == 200
    assert run.json()["status"] == "FAILED"
    assert run.json()["error_code"] == "DEPENDENCY_UNAVAILABLE"

    events = client.get(
        f"/api/v1/assistant/runs/{run_id}/events", headers=headers
    )
    assert events.status_code == 200
    assert "RUN_FAILED" in events.text
    assert "reasoning_content" not in events.text
