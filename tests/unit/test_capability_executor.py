"""Phase 4.0 tests for CapabilityExecutor."""

from __future__ import annotations

from typing import Any

import pytest
from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.execution.capability_executor import CapabilityExecutor
from greenbook_agent_core.planning.contracts import PlanStep


@pytest.fixture
def registry() -> CapabilityRegistry:
    return CapabilityRegistry()


# ── mock tool_handler ─────────────────────────────────────────────

def _make_handler(responses: dict[str, dict[str, Any]]) -> Any:
    """Return an async tool_handler that returns canned responses."""
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL", "message": f"Unknown: {tool_name}"}
    return handler


# ── Scenario 1: SEARCH_COMMUNITY → search tool ────────────────────

@pytest.mark.asyncio
async def test_search_community_calls_search_tool(registry: CapabilityRegistry) -> None:
    handler = _make_handler({
        "community.search_public_posts": {
            "ok": True,
            "code": "",
            "data": {"items": [{"post_id": "p1", "title": "Java 101"}], "total": 1},
        },
    })
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="SEARCH_COMMUNITY",
        ordinal=1,
        description="Search Java posts",
        output_artifact_type="SEARCH_RESULT",
    )
    result = await executor.execute_step(step)

    assert result.ok is True
    assert result.capability == "SEARCH_COMMUNITY"
    assert result.tool_name == "community.search_public_posts"
    assert result.error_code == ""
    assert result.artifact is not None
    assert result.artifact.artifact_type == "SEARCH_RESULT"
    # SEARCH_RESULT is a collection — no single resource_id
    assert result.artifact.summary == "Java 101"
    assert result.artifact.resource_refs == [{"kind": "POST", "resource_id": "p1"}]


@pytest.mark.asyncio
async def test_search_community_passes_constraints_as_args(registry: CapabilityRegistry) -> None:
    captured: dict[str, Any] = {}

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        captured["tool"] = tool_name
        captured["args"] = tool_args
        return {"ok": True, "code": "", "data": {"items": [], "total": 0}}

    executor = CapabilityExecutor(registry, handler)
    step = PlanStep(
        capability="SEARCH_COMMUNITY",
        ordinal=1,
        output_artifact_type="SEARCH_RESULT",
        constraints={"query": "Java", "sort": "hot"},
    )
    await executor.execute_step(step)

    assert captured["tool"] == "community.search_public_posts"
    assert captured["args"]["query"] == "Java"
    assert captured["args"]["sort"] == "hot"


# ── Scenario 2: GENERATE_CONTENT → creator tool ───────────────────

@pytest.mark.asyncio
async def test_generate_content_calls_create_draft(registry: CapabilityRegistry) -> None:
    handler = _make_handler({
        "content.create_draft": {
            "ok": True,
            "code": "",
            "data": {
                "draft_id": "draft-99",
                "title": "Java from Zero to Hero",
                "content": "markdown...",
                "status": "DRAFT",
            },
        },
    })
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="GENERATE_CONTENT",
        ordinal=3,
        description="Generate a Java article",
        output_artifact_type="DRAFT",
        constraints={"title": "Java Guide", "instruction": "Write about Java basics"},
    )
    result = await executor.execute_step(step)

    assert result.ok is True
    assert result.capability == "GENERATE_CONTENT"
    assert result.tool_name == "content.create_draft"
    assert result.artifact is not None
    assert result.artifact.artifact_type == "DRAFT"
    assert result.artifact.resource_id == "draft-99"
    assert result.artifact.summary == "Java from Zero to Hero"


# ── Scenario 3: unknown capability → MISSING_TOOL ─────────────────

@pytest.mark.asyncio
async def test_unknown_capability_returns_error(registry: CapabilityRegistry) -> None:
    handler = _make_handler({})
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="NONEXISTENT",
        ordinal=1,
    )
    result = await executor.execute_step(step)

    assert result.ok is False
    assert result.error_code == "UNKNOWN_CAPABILITY"


@pytest.mark.asyncio
async def test_llm_step_skips_tool_and_returns_artifact(
    registry: CapabilityRegistry,
) -> None:
    """ANALYZE_CONTENT_PATTERNS is a pure-LLM step — no tool call."""
    call_count = 0

    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"ok": True, "code": "", "data": {}}

    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="ANALYZE_CONTENT_PATTERNS",
        ordinal=2,
        description="Analyze patterns",
        output_artifact_type="ANALYSIS_REPORT",
    )
    result = await executor.execute_step(step)

    assert result.ok is True
    assert result.tool_name == "(llm)"
    assert call_count == 0  # tool handler never called
    assert result.artifact is not None
    assert result.artifact.artifact_type == "ANALYSIS_REPORT"


# ── Scenario 4: side_effect capability → approval info ────────────

@pytest.mark.asyncio
async def test_publish_now_returns_approval_required(registry: CapabilityRegistry) -> None:
    handler = _make_handler({
        "publication.publish_now": {
            "ok": False,
            "code": "APPROVAL_REQUIRED",
            "message": "publish_now requires explicit user approval",
            "user_message": "需要用户确认",
        },
    })
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="PUBLISH_NOW",
        ordinal=2,
        description="Publish immediately",
    )
    result = await executor.execute_step(step)

    assert result.ok is False
    assert result.error_code == "APPROVAL_REQUIRED"
    assert result.approval_required is True


@pytest.mark.asyncio
async def test_side_effect_capability_tool_failure_is_reported(
    registry: CapabilityRegistry,
) -> None:
    handler = _make_handler({
        "publication.schedule": {
            "ok": False,
            "code": "BUSINESS_REJECTED",
            "message": "Draft is not publishable",
            "user_message": "草稿状态不支持发布",
            "retryable": False,
        },
    })
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="SCHEDULE_PUBLISH",
        ordinal=5,
        description="Schedule for publication",
        output_artifact_type="SCHEDULE",
    )
    result = await executor.execute_step(step)

    assert result.ok is False
    assert result.error_code == "BUSINESS_REJECTED"
    assert result.retryable is False
    assert result.tool_name == "publication.schedule"


# ── edge cases ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_handler_exception_is_caught(registry: CapabilityRegistry) -> None:
    async def handler(tool_name: str, tool_args: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("boom")

    executor = CapabilityExecutor(registry, handler)
    step = PlanStep(capability="SEARCH_COMMUNITY", ordinal=1)

    result = await executor.execute_step(step)

    assert result.ok is False
    assert result.error_code == "TOOL_EXECUTION_FAILED"
    assert result.retryable is False


@pytest.mark.asyncio
async def test_improve_content_calls_revise_draft(registry: CapabilityRegistry) -> None:
    handler = _make_handler({
        "content.revise_draft": {
            "ok": True,
            "code": "",
            "data": {
                "draft_id": "draft-1",
                "title": "Revised Java Guide",
                "status": "DRAFT",
            },
        },
    })
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="IMPROVE_CONTENT",
        ordinal=1,
        output_artifact_type="DRAFT",
        constraints={"draft_id": "draft-1", "revision_instruction": "Make it better"},
    )
    result = await executor.execute_step(step)

    assert result.ok is True
    assert result.tool_name == "content.revise_draft"
    assert result.artifact is not None
    assert result.artifact.resource_id == "draft-1"


@pytest.mark.asyncio
async def test_result_without_data_has_no_artifact(registry: CapabilityRegistry) -> None:
    handler = _make_handler({
        "community.search_public_posts": {
            "ok": True,
            "code": "",
            "data": None,
        },
    })
    executor = CapabilityExecutor(registry, handler)
    step = PlanStep(
        capability="SEARCH_COMMUNITY",
        ordinal=1,
        output_artifact_type="SEARCH_RESULT",
    )
    result = await executor.execute_step(step)
    assert result.ok is True
    assert result.artifact is None


@pytest.mark.asyncio
async def test_cancel_schedule_calls_correct_tool(registry: CapabilityRegistry) -> None:
    handler = _make_handler({
        "publication.cancel_schedule": {
            "ok": True,
            "code": "",
            "data": {"schedule_id": "s1", "status": "CANCELLED"},
        },
    })
    executor = CapabilityExecutor(registry, handler)

    step = PlanStep(
        capability="CANCEL_SCHEDULE",
        ordinal=1,
        constraints={"schedule_id": "s1"},
    )
    result = await executor.execute_step(step)

    assert result.ok is True
    assert result.tool_name == "publication.cancel_schedule"
