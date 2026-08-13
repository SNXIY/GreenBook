from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_mcp_server.tool_registry import (
    list_tools,
    validate_registered_tool_contracts,
)
from greenbook_mcp_server.tool_schemas import CreateDraftArguments


def test_every_runtime_tool_has_one_complete_contract() -> None:
    validate_registered_tool_contracts()
    tools = list_tools()

    assert len(tools) >= 16
    assert len({tool.name for tool in tools}) == len(tools)
    for tool in tools:
        assert tool.input_schema is not None
        assert tool.output_schema is not None
        assert tool.policy.permission is not None
        assert tool.policy.retry_policy is not None
        assert tool.policy.side_effect is not None
        assert tool.capability
        assert tool.handler is not None


def test_create_draft_contract_matches_creator_handler_boundary() -> None:
    arguments = CreateDraftArguments.model_validate(
        {"title": "Java 学习路线", "instruction": "生成一篇 Java 学习路线文章"}
    )
    assert arguments.instruction.startswith("生成")

    with pytest.raises(ValueError):
        CreateDraftArguments.model_validate(
            {"title": "Java 学习路线", "content": "不应作为 handler 输入"}
        )


def test_exported_contracts_include_schema_and_policy_metadata() -> None:
    server = GreenBookMCPServer(java=object(), creator=object())
    definitions = server.get_tool_definitions()

    assert len(definitions) == len(list_tools())
    create = next(item for item in definitions if item["name"] == "content.create_draft")
    assert create["parameters"]["required"] == ["title", "instruction"]
    assert "content" not in create["parameters"]["properties"]
    assert create["operations"] == ["CREATE_CONTENT"]
    assert create["output_schema"]["properties"]["ok"]["type"] == "boolean"
    assert create["risk"] == "IDEMPOTENT_WRITE"
    assert create["permission"]["required_scopes"] == []
    assert create["side_effect"]["has_side_effect"] is True
    assert create["retry_policy"]["max_attempts"] == 2


@pytest.mark.asyncio
async def test_output_contract_is_validated_after_handler_execution() -> None:
    java = SimpleNamespace(
        get_account_summary=AsyncMock(
            return_value=ToolResult.success(
                SimpleNamespace(model_dump=lambda mode: {"views": 1})
            )
        )
    )
    server = GreenBookMCPServer(java=java, creator=object())
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "analytics.get_account_summary",
        auth=auth,
        session=SimpleNamespace(
            conversation_id="conversation-1",
            pending_approval=None,
            active_draft_id=None,
            active_schedule_id=None,
            resolve_active_draft_id=lambda: (None, []),
            resolve_active_schedule_id=lambda: (None, []),
            record_entity=lambda **_: None,
        ),
    )

    assert result["ok"] is True
    assert result["data"] == {"views": 1}
