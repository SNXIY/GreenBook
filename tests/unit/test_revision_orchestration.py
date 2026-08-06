from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_core.agent import CommunityOperationsAssistant
from greenbook_assistant_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from starlette.testclient import TestClient

from apps.assistant_api.greenbook_assistant_api.api.routes import (
    _normalize_update_schedule_tool_args,
)
from apps.assistant_api.greenbook_assistant_api.main import create_app


def _session() -> SessionContext:
    return SessionContext(
        conversation_id="conv-1",
        user_id="u1",
        tenant_id="t1",
        active_draft_id="draft-1",
        active_schedule_id="schedule-1",
    )


@pytest.mark.asyncio
async def test_revision_then_schedule_is_ordered_and_reasoning_is_preserved() -> None:
    revise_call = SimpleNamespace(
        id="revise-call",
        function=SimpleNamespace(
            name="content_revise_draft",
            arguments=(
                '{"draft_id":"draft-1",'
                '"revision_instruction":"增加实战经验"}'
            ),
        ),
    )
    update_call = SimpleNamespace(
        id="update-call",
        function=SimpleNamespace(
            name="publication_update_schedule",
            arguments=(
                '{"schedule_id":"schedule-1",'
                '"run_at":"2026-08-06T14:53:00Z"}'
            ),
        ),
    )
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=None,
                reasoning_content="revise first",
                tool_calls=[revise_call],
            ))],
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=None,
                reasoning_content="update second",
                tool_calls=[update_call],
            ))],
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="操作完成",
                reasoning_content=None,
                tool_calls=None,
            ))],
        ),
    ]
    create = AsyncMock(side_effect=responses)
    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = CommunityOperationsAssistant(
        llm=llm,
        model="thinking-test-model",
        tools_schema=[
            {"type": "function", "function": {"name": "content_revise_draft"}},
            {"type": "function", "function": {"name": "publication_update_schedule"}},
        ],
    )
    calls: list[str] = []

    async def tool_handler(
        name: str,
        args: dict[str, object],
        session: SessionContext,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, object]:
        calls.append(name)
        if name == "content_revise_draft":
            assert args["draft_id"] == "draft-1"
            assert args["revision_instruction"] == "增加实战经验"
            return ToolResult.success(
                {"draft_id": "draft-1", "title": "Java 实战"}
            ).model_dump(mode="json")
        assert name == "publication_update_schedule"
        assert args["schedule_id"] == "schedule-1"
        return ToolResult.success(
            {
                "draft_id": "draft-1",
                "schedule_id": "schedule-1",
                "run_at": "2026-08-06T14:53:00Z",
                "timezone": "Asia/Shanghai",
                "status": "SCHEDULED",
            }
        ).model_dump(mode="json")

    result = await agent.run(
        user_message=(
            "修改帖子的内容，增加一些实战经验，然后发布时间调整为五分钟之后"
        ),
        session=_session(),
        tool_handler=tool_handler,
        run_id="run-1",
    )

    assert calls == ["content_revise_draft", "publication_update_schedule"]
    second_request = create.call_args_list[1].kwargs
    assert [
        item["function"]["name"] for item in second_request["tools"]
    ] == ["publication_update_schedule"]
    first_assistant = next(
        item for item in second_request["messages"] if item.get("role") == "assistant"
    )
    assert first_assistant["reasoning_content"] == "revise first"
    assert result["content"] == "操作完成"


def test_update_time_uses_message_received_at_and_utc_java_value() -> None:
    received_at = datetime(
        2026,
        8,
        6,
        22,
        48,
        tzinfo=timezone(timedelta(hours=8)),
    )
    normalized = _normalize_update_schedule_tool_args(
        {"schedule_id": "schedule-1", "run_at": "wrong"},
        user_message="发布时间调整为五分钟之后",
        timezone_name="Asia/Shanghai",
        now=received_at,
    )
    assert normalized["run_at"] == "2026-08-06T14:53:00Z"
    assert "timezone_name" not in normalized


def test_legacy_publish_at_is_normalized_before_relative_time_is_applied() -> None:
    received_at = datetime(
        2026,
        8,
        6,
        23,
        23,
        tzinfo=timezone(timedelta(hours=8)),
    )
    normalized = _normalize_update_schedule_tool_args(
        {"schedule_id": "schedule-1", "publish_at": "stale"},
        user_message=(
            "\u53ea\u628a\u521a\u624d\u5e16\u5b50\u7684\u53d1\u5e03\u65f6\u95f4"
            "\u6539\u4e3a\u4e94\u5206\u949f\u4e4b\u540e\uff0c\u4e0d\u8981\u518d\u6b21\u4fee\u6539\u5185\u5bb9"
        ),
        timezone_name="Asia/Shanghai",
        now=received_at,
    )

    assert normalized["run_at"] == "2026-08-06T15:28:00Z"
    assert "publish_at" not in normalized


def test_revision_success_then_schedule_failure_is_partial_failure() -> None:
    revise_call = SimpleNamespace(
        id="revise-call",
        function=SimpleNamespace(
            name="content_revise_draft",
            arguments='{"draft_id":"draft-1","revision_instruction":"增加实战经验"}',
        ),
    )
    update_call = SimpleNamespace(
        id="update-call",
        function=SimpleNamespace(
            name="publication_update_schedule",
            arguments='{"schedule_id":"schedule-1","run_at":"wrong"}',
        ),
    )
    final = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content="时间调整失败",
            reasoning_content=None,
            tool_calls=None,
        ))],
    )
    llm = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(side_effect=[
                    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content=None,
                        reasoning_content="revise",
                        tool_calls=[revise_call],
                    ))]),
                    SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                        content=None,
                        reasoning_content="schedule",
                        tool_calls=[update_call],
                    ))]),
                    final,
                ])
            )
        )
    )

    class SequenceMCP:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def execute_tool(self, name: str, **kwargs: object) -> dict[str, object]:
            self.calls.append(name)
            if name == "content.revise_draft":
                return ToolResult.success(
                    {"draft_id": "draft-1", "title": "Java 实战"}
                ).model_dump(mode="json")
            return ToolResult.business_rejected(
                "schedule is no longer SCHEDULED",
                user_message="定时任务当前状态不支持修改。",
            ).model_dump(mode="json")

    mcp = SequenceMCP()
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
    app.state.mcp = mcp
    app.state.model = "thinking-test-model"

    client = TestClient(app)
    headers = {"Authorization": "Bearer test-access"}
    conversation = client.post(
        "/api/v1/assistant/conversations",
        json={"title": "partial"},
        headers=headers,
    )
    conversation_id = conversation.json()["conversation_id"]
    app.state.conversation_store[conversation_id]["active_draft_id"] = "draft-1"
    app.state.conversation_store[conversation_id]["active_schedule_id"] = "schedule-1"

    response = client.post(
        f"/api/v1/assistant/conversations/{conversation_id}/messages",
        json={
            "content": "修改帖子的内容，增加一些实战经验，然后发布时间调整为五分钟之后"
        },
        headers=headers,
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "BUSINESS_REJECTED"
    assert detail["state"]["phase"] == "PARTIAL_FAILURE"
    assert detail["state"]["draft_revision"] == "COMPLETED"
    assert detail["state"]["schedule_update"] == "FAILED"
    assert "草稿内容已修改" in detail["message"]
    assert mcp.calls == ["content.revise_draft", "publication.update_schedule"]
    run = client.get(f"/api/v1/assistant/runs/{detail['run_id']}", headers=headers)
    assert run.json()["status"] == "PARTIAL_FAILURE"


@pytest.mark.asyncio
async def test_partial_success_recovery_exposes_only_schedule_update() -> None:
    update_call = SimpleNamespace(
        id="recovery-update-call",
        function=SimpleNamespace(
            name="publication_update_schedule",
            arguments=(
                '{"schedule_id":"schedule-1",'
                '"publish_at":"2026-08-06T15:28:00Z"}'
            ),
        ),
    )
    responses = [
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=None,
                reasoning_content="schedule recovery",
                tool_calls=[update_call],
            ))],
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content="宸插彧鏇存柊瀹氭椂浠诲姟",
                reasoning_content=None,
                tool_calls=None,
            ))],
        ),
    ]
    create = AsyncMock(side_effect=responses)
    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = CommunityOperationsAssistant(
        llm=llm,
        model="thinking-test-model",
        tools_schema=[
            {"type": "function", "function": {"name": "content_revise_draft"}},
            {"type": "function", "function": {"name": "publication_update_schedule"}},
        ],
    )
    calls: list[str] = []

    async def tool_handler(
        name: str,
        args: dict[str, object],
        session: SessionContext,
        run_id: str,
        tool_call_id: str,
    ) -> dict[str, object]:
        calls.append(name)
        assert name == "publication_update_schedule"
        assert args["schedule_id"] == "schedule-1"
        return ToolResult.success(
            {
                "draft_id": "draft-1",
                "schedule_id": "schedule-1",
                "run_at": "2026-08-06T15:28:00Z",
                "timezone": "Asia/Shanghai",
                "status": "SCHEDULED",
                "version": 5,
            }
        ).model_dump(mode="json")

    result = await agent.run(
        user_message="只把刚才帖子的发布时间改为五分钟之后，不要再次修改内容",
        session=_session(),
        tool_handler=tool_handler,
        run_id="recovery-run",
    )

    assert calls == ["publication_update_schedule"]
    assert [
        item["function"]["name"]
        for item in create.call_args_list[0].kwargs["tools"]
    ] == ["publication_update_schedule"]
    assert result["content"] == "宸插彧鏇存柊瀹氭椂浠诲姟"
