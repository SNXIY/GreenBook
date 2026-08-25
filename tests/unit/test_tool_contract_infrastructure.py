from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from greenbook_contracts.identity import AuthContext
from greenbook_contracts.tool_result import ToolResult
from greenbook_mcp_server.server import GreenBookMCPServer
from greenbook_mcp_server.tool_registry import (
    get_tool,
    list_tools,
    validate_registered_tool_contracts,
)
from greenbook_mcp_server.tool_schemas import CreateDraftArguments


def test_every_runtime_tool_has_one_complete_contract() -> None:
    validate_registered_tool_contracts()
    tools = list_tools()

    assert len(tools) >= 14
    assert len({tool.name for tool in tools}) == len(tools)
    for tool in tools:
        assert tool.input_schema is not None
        assert tool.output_schema is not None
        assert tool.policy.permission is not None
        assert tool.policy.retry_policy is not None
        assert tool.policy.side_effect is not None
        assert tool.capability
        assert tool.handler is not None


def test_create_draft_contract_matches_handler_boundary() -> None:
    arguments = CreateDraftArguments.model_validate(
        {"title": "Java 学习路线", "instruction": "生成一篇 Java 学习路线文章"}
    )
    assert arguments.instruction.startswith("生成")

    with pytest.raises(ValueError):
        CreateDraftArguments.model_validate(
            {"title": "Java 学习路线", "content": "不应作为 handler 输入"}
        )


def test_exported_contracts_include_schema_and_policy_metadata() -> None:
    server = GreenBookMCPServer(java=object())
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
    server = GreenBookMCPServer(java=java)
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


@pytest.mark.asyncio
async def test_argument_validation_is_not_transport_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    async def should_not_run(*_args: object, **_kwargs: object) -> ToolResult[object]:
        calls["n"] += 1
        return ToolResult.internal_error("validation bypassed the boundary")

    server = GreenBookMCPServer(java=object())
    monkeypatch.setattr(get_tool("content.create_draft"), "handler", should_not_run)
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "content.create_draft",
        auth=auth,
        session=_session(),
        title="Java",
        # instruction is intentionally omitted: no handler/Java call may run.
    )

    assert result["ok"] is False
    assert result["code"] == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert result["retryable"] is False
    assert result["request_sent"] is False
    assert result["state"]["side_effect_started"] is False
    assert calls["n"] == 0


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        conversation_id="conversation-1",
        pending_approval=None,
        active_draft_id=None,
        active_schedule_id=None,
        resolve_active_draft_id=lambda: (None, []),
        resolve_active_schedule_id=lambda: (None, []),
        record_entity=lambda **_: None,
    )


@pytest.mark.asyncio
async def test_side_effect_handler_exception_is_result_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrapper crash cannot prove that a write did not reach Java."""

    async def broken_handler(
        _ctx: object,
        draft_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> ToolResult[object]:
        del draft_id, title, content
        raise RuntimeError("crashed after a possible downstream write")

    server = GreenBookMCPServer(java=object())
    monkeypatch.setattr(get_tool("content.update_draft"), "handler", broken_handler)
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "content.update_draft",
        auth=auth,
        session=_session(),
        draft_id="draft-1",
        title="revised title",
    )

    assert result["ok"] is False
    assert result["code"] == "RESULT_UNKNOWN"
    assert result["request_sent"] is None
    assert result["state"]["side_effect_started"] is True
    assert result["state"]["safe_to_retry"] is False


@pytest.mark.asyncio
async def test_side_effect_output_contract_failure_is_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid post-write projection is also not proof of a failed write."""

    async def malformed_handler(
        _ctx: object,
        draft_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> dict[str, object]:
        del draft_id, title, content
        return {"not": "a ToolResult"}

    server = GreenBookMCPServer(java=object())
    monkeypatch.setattr(get_tool("content.update_draft"), "handler", malformed_handler)
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "content.update_draft",
        auth=auth,
        session=_session(),
        draft_id="draft-1",
        title="revised title",
    )

    assert result["code"] == "RESULT_UNKNOWN"
    assert result["request_sent"] is None
    assert result["state"]["phase"] == "POST_EXECUTION_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_side_effect_success_requires_verified_operation_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare success return must not become a user-visible completion."""

    async def unverified_success_handler(
        _ctx: object,
        draft_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> ToolResult[dict[str, str]]:
        del draft_id, title, content
        return ToolResult.success({"draft_id": "draft-1"})

    server = GreenBookMCPServer(java=object())
    monkeypatch.setattr(
        get_tool("content.update_draft"),
        "handler",
        unverified_success_handler,
    )
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "content.update_draft",
        auth=auth,
        session=_session(),
        draft_id="draft-1",
        title="revised title",
    )

    assert result["ok"] is False
    assert result["code"] == "RESULT_UNKNOWN"
    assert result["state"]["phase"] == "POSTCONDITION_EVIDENCE_MISSING"


@pytest.mark.asyncio
async def test_side_effect_failure_before_write_is_not_result_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deterministic pre-write failure must not enter reconciliation."""

    async def failed_handler(
        _ctx: object,
        draft_id: str | None = None,
        title: str | None = None,
        content: str | None = None,
    ) -> ToolResult[object]:
        del draft_id, title, content
        return ToolResult.internal_error("generation failed before Java write")

    server = GreenBookMCPServer(java=object())
    monkeypatch.setattr(get_tool("content.update_draft"), "handler", failed_handler)
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "content.update_draft",
        auth=auth,
        session=_session(),
        draft_id="draft-1",
        title="revised title",
    )

    assert result["ok"] is False
    assert result["code"] == "INTERNAL_ERROR"
    assert result["request_sent"] is False


@pytest.mark.asyncio
async def test_read_handler_exception_remains_a_normal_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken_handler(_ctx: object) -> ToolResult[object]:
        raise RuntimeError("read handler crash")

    server = GreenBookMCPServer(java=object())
    monkeypatch.setattr(get_tool("analytics.get_account_summary"), "handler", broken_handler)
    auth = AuthContext(user_id="user-1", tenant_id="tenant-1", raw_access_token="token")

    result = await server.execute_tool(
        "analytics.get_account_summary",
        auth=auth,
        session=_session(),
    )

    assert result["ok"] is False
    assert result["code"] == "INTERNAL_ERROR"
    assert result["request_sent"] is False
    assert result["state"]["side_effect_started"] is False
