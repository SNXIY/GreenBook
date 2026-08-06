from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_core.context import SessionContext
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_java_client.models import (
    DraftResponse,
    ScheduledPublicationResponse,
)
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_mcp_server.tool_registry import validate_registered_tool_contracts
from greenbook_mcp_server.tool_schemas import (
    ReviseDraftArguments,
    openai_parameters,
)
from greenbook_mcp_server.tools.content import revise_draft
from greenbook_mcp_server.tools.publication import update_schedule


def _auth() -> AuthContext:
    return AuthContext(user_id="u1", tenant_id="t1", raw_access_token="token")


def _session() -> SessionContext:
    return SessionContext(
        conversation_id="conv-1",
        user_id="u1",
        tenant_id="t1",
        active_draft_id="draft-1",
        active_schedule_id="schedule-1",
    )


def test_revision_schema_and_handler_are_single_source() -> None:
    validate_registered_tool_contracts()
    schema = openai_parameters(ReviseDraftArguments)
    assert set(schema["properties"]) == {
        "draft_id",
        "revision_instruction",
        "title",
        "expected_version",
    }
    assert schema["required"] == ["draft_id", "revision_instruction"]
    assert schema["additionalProperties"] is False
    assert "content" not in schema["properties"]


def test_revision_schema_accepts_legacy_instruction_alias() -> None:
    arguments = ReviseDraftArguments.model_validate(
        {"draft_id": "draft-1", "instruction": "增加实战经验"}
    )
    assert arguments.revision_instruction == "增加实战经验"


@pytest.mark.asyncio
async def test_unknown_revision_content_is_rejected_before_handler() -> None:
    server = GreenBookMCPServer(java=object(), creator=object())
    result = await server.execute_tool(
        "content.revise_draft",
        auth=_auth(),
        session=_session(),
        draft_id="draft-1",
        content="这不是完整的新正文",
    )

    assert result["ok"] is False
    assert result["code"] == "INVALID_TOOL_ARGUMENT"
    assert result["request_sent"] is False
    assert result["state"] == {
        "phase": "PRE_EXECUTION_VALIDATION_FAILED",
        "downstream_called": False,
        "side_effect_started": False,
        "safe_to_retry": True,
    }


@pytest.mark.asyncio
async def test_missing_revision_target_is_rejected_before_handler() -> None:
    server = GreenBookMCPServer(java=object(), creator=object())
    result = await server.execute_tool(
        "content.revise_draft",
        auth=_auth(),
        session=_session(),
        revision_instruction="增加实战经验",
    )

    assert result["ok"] is False
    assert result["code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert result["request_sent"] is False
    assert result["state"]["safe_to_retry"] is True


@pytest.mark.asyncio
async def test_creator_revision_maps_final_content_to_java_update_once() -> None:
    current = DraftResponse.model_validate(
        {
            "draftId": "draft-1",
            "title": "Java 并发",
            "content": "旧正文",
            "updatedAt": "2026-08-06T14:40:00Z",
            "version": 2,
            "status": "draft",
        }
    )
    updated = DraftResponse.model_validate(
        {
            "draftId": "draft-1",
            "title": "Java 并发实战",
            "content": "完整的新正文，包含实战经验",
            "updatedAt": "2026-08-06T14:45:00Z",
            "version": 3,
            "status": "draft",
        }
    )
    java = SimpleNamespace(
        get_draft=AsyncMock(
            side_effect=[ToolResult.success(current), ToolResult.success(updated)]
        ),
        update_draft=AsyncMock(return_value=ToolResult.success(updated)),
        get_schedule=AsyncMock(
            return_value=ToolResult.success(
                ScheduledPublicationResponse.model_validate(
                    {
                        "scheduleId": "schedule-1",
                        "draftId": "draft-1",
                        "status": "SCHEDULED",
                        "version": 4,
                    }
                )
            )
        ),
    )
    creator = SimpleNamespace(
        create_task=AsyncMock(return_value=ToolResult.success({"task_id": "task-1"})),
        wait_for_completion=AsyncMock(
            return_value=ToolResult.success({"final_artifact_id": "artifact-1"})
        ),
        get_artifact=AsyncMock(
            return_value=ToolResult.success(
                {
                    "content": {
                        "document": {
                            "title": "Java 并发实战",
                            "body_markdown": "完整的新正文，包含实战经验",
                            "description": "摘要",
                        }
                    }
                }
            )
        ),
    )
    from greenbook_mcp_server.context import ToolContext

    context = ToolContext(
        auth=_auth(),
        session=_session(),
        java=java,
        creator=creator,
        conversation_id="conv-1",
        agent_run_id="run-1",
        tool_call_id="call-1",
    )

    result = await revise_draft(
        context,
        draft_id="draft-1",
        revision_instruction="增加实战经验",
    )

    assert result.ok is True
    creator.create_task.assert_awaited_once()
    creator.get_artifact.assert_awaited_once()
    java.update_draft.assert_awaited_once()
    request = java.update_draft.await_args.args[1]
    assert request.content == "完整的新正文，包含实战经验"
    assert request.expected_version == "2026-08-06T14:40:00+00:00"
    assert result.data["draft_id"] == "draft-1"


@pytest.mark.asyncio
async def test_update_schedule_updates_same_id_and_verifies_scheduled() -> None:
    current = ScheduledPublicationResponse.model_validate(
        {
            "scheduleId": "schedule-1",
            "draftId": "draft-1",
            "runAt": "2026-08-06T14:48:00Z",
            "timezone": "Asia/Shanghai",
            "status": "SCHEDULED",
            "version": 4,
        }
    )
    updated = ScheduledPublicationResponse.model_validate(
        {
            "scheduleId": "schedule-1",
            "draftId": "draft-1",
            "runAt": "2026-08-06T14:53:00Z",
            "timezone": "Asia/Shanghai",
            "status": "SCHEDULED",
            "version": 5,
        }
    )
    java = SimpleNamespace(
        get_schedule=AsyncMock(
            side_effect=[ToolResult.success(current), ToolResult.success(updated)]
        ),
        update_schedule=AsyncMock(return_value=ToolResult.success(updated)),
    )
    from greenbook_mcp_server.context import ToolContext

    context = ToolContext(auth=_auth(), session=_session(), java=java, creator=object())
    result = await update_schedule(
        context,
        schedule_id="schedule-1",
        run_at="2026-08-06T14:53:00Z",
    )

    assert result.ok is True
    java.update_schedule.assert_awaited_once()
    assert java.update_schedule.await_args.args[0] == "schedule-1"
    request = java.update_schedule.await_args.args[1]
    assert request.run_at == "2026-08-06T14:53:00Z"
    assert request.version == 4
    assert result.data["schedule_id"] == "schedule-1"
    assert result.data["status"] == "SCHEDULED"
