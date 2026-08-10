"""Phase 5.1 + 5.2 tests for RuntimeAgentService pipeline."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext, TaskContext
from greenbook_assistant_api.services.runtime_agent_service import (
    RuntimeAgentService,
)
from greenbook_assistant_core.execution.execution_queue import (
    ExecutionQueue,
    ExecutionQueueStatus,
)
from greenbook_assistant_core.task.models import ArtifactRef, TaskIntent


def _ctx(**kw: Any) -> RuntimeContext:
    """Build a RuntimeContext for a CREATE_CONTENT scenario."""
    intent = type("_Intent", (), {
        "goal_category": kw.pop("goal_category", "CREATE_CONTENT"),
        "relation": kw.pop("relation", "NEW_TASK"),
        "requirements": kw.pop("requirements", [{"type": "CREATE"}]),
    })()
    mcp = kw.pop("mcp", None) or _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-1", "title": "Java Guide"},
        },
    })
    return RuntimeContext(
        run_id="run-r1",
        trace_id="trace-r1",
        task_id="task-r1",
        user_id="u1",
        task_intent=intent,
        task_context=TaskContext(
            task_id="task-r1",
            goal=str(getattr(intent, "goal", "") or ""),
            task_intent=intent,
        ),
        user_message=kw.pop("user_message", "帮我写一篇Java学习文章"),
        mcp=mcp,
        session=None,
        **kw,
    )


def _mock_mcp(responses: dict[str, dict[str, Any]]) -> AsyncMock:
    """Build an MCP mock that returns different responses per tool."""
    mcp = AsyncMock()

    async def execute_tool(tool_name: str, **kw: Any) -> dict[str, Any]:
        if tool_name in responses:
            return dict(responses[tool_name])
        return {"ok": False, "code": "UNKNOWN_TOOL",
                "message": f"Unexpected: {tool_name}"}

    mcp.execute_tool = execute_tool
    return mcp


# ── Scenario: CREATE_CONTENT single-step pipeline ─────────────────

@pytest.mark.asyncio
async def test_create_content_pipeline_completes() -> None:
    service = RuntimeAgentService()
    ctx = _ctx()
    result = await service.execute(ctx)

    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.execution_path == "runtime"
    assert result.draft_id == "draft-1"
    assert result.started_execution is True
    assert result.side_effect_committed is True
    assert len(result.artifact_ids) >= 1
    assert len(result.events) >= 3  # TASK_CREATED + PLAN_CREATED + EXECUTION_STARTED + …
    assert result.tool_rounds >= 1
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_pipeline_generates_trace_events() -> None:
    service = RuntimeAgentService()
    result = await service.execute(_ctx())

    event_types = {e["event"] for e in result.events}
    assert "TASK_CREATED" in event_types
    assert "PLAN_CREATED" in event_types
    assert "EXECUTION_STARTED" in event_types
    assert "EXECUTION_COMPLETED" in event_types


@pytest.mark.asyncio
async def test_pipeline_produces_draft() -> None:
    service = RuntimeAgentService()
    result = await service.execute(_ctx())

    assert result.draft_id is not None
    assert result.draft_id == "draft-1"
    assert len(result.artifact_ids) == 1


@pytest.mark.asyncio
async def test_pipeline_fallback_allowed() -> None:
    """Runtime failures should allow fallback to Legacy."""
    service = RuntimeAgentService()
    result = await service.execute(_ctx())

    assert result.fallback_allowed is True


# ── Phase 5.2: Multi-step DAG (CREATE + PUBLISH) ────────────────────

@pytest.mark.asyncio
async def test_create_and_publish_multi_step() -> None:
    """CREATE + PUBLISH → 3-step DAG: GENERATE → VALIDATE → SCHEDULE."""
    draft_id = "draft-multi-1"
    schedule_id = "schedule-multi-1"
    call_log: list[str] = []

    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": draft_id, "title": "Java Guide"},
        },
        "publication.schedule": {
            "ok": True, "code": "",
            "data": {
                "schedule_id": schedule_id, "draft_id": draft_id,
                "run_at": "2026-08-08T00:00:00Z", "timezone": "Asia/Shanghai",
                "status": "SCHEDULED",
            },
        },
    })
    # Override to capture calls
    orig = mcp.execute_tool

    async def logging_execute(tool_name: str, **kw: Any) -> dict[str, Any]:
        call_log.append(tool_name)
        return await orig(tool_name, **kw)

    mcp.execute_tool = logging_execute

    ctx = _ctx(
        mcp=mcp,
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
        user_message="帮我写一篇Java学习文章，标题新颖一点，五分钟后发布",
    )
    service = RuntimeAgentService()
    result = await service.execute(ctx)

    # ── Overall result ──
    assert result.success is True
    assert result.status == "COMPLETED"
    assert result.draft_id == draft_id
    assert result.side_effect_committed is True

    # ── Both tools called ──
    assert "content.create_draft" in call_log
    assert "publication.schedule" in call_log

    # ── Artifacts ──
    assert len(result.artifact_ids) >= 2  # DRAFT + SCHEDULE (VALIDATE skipped)
    event_types = {e["event"] for e in result.events}

    # ── Full trace ──
    assert "TASK_CREATED" in event_types
    assert "PLAN_CREATED" in event_types
    assert "EXECUTION_STARTED" in event_types
    assert "STEP_STARTED" in event_types
    assert "TOOL_INVOKED" in event_types
    assert "TOOL_COMPLETED" in event_types
    assert "ARTIFACT_CREATED" in event_types
    assert "STEP_COMPLETED" in event_types
    assert "EXECUTION_COMPLETED" in event_types

    # ── STEP_STARTED x2 (GENERATE + SCHEDULE, VALIDATE is LLM-only) ──
    step_started = [e for e in result.events if e["event"] == "STEP_STARTED"]
    assert len(step_started) >= 2

    # ── TOOL_INVOKED x2 ──
    tool_invoked = [e for e in result.events if e["event"] == "TOOL_INVOKED"]
    assert len(tool_invoked) >= 2

    # ── ARTIFACT_CREATED x2 ──
    artifact_events = [e for e in result.events if e["event"] == "ARTIFACT_CREATED"]
    assert len(artifact_events) >= 2

    # ── No FAILED events ──
    assert "STEP_FAILED" not in event_types
    assert "TOOL_FAILED" not in event_types
    assert "EXECUTION_FAILED" not in event_types


@pytest.mark.asyncio
async def test_schedule_step_receives_draft_id_from_upstream() -> None:
    """Verify that the schedule step gets draft_id from the GENERATE step's artifact."""
    captured_schedule_args: dict[str, Any] = {}

    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "upstream-draft", "title": "X"},
        },
        "publication.schedule": {
            "ok": True, "code": "",
            "data": {"schedule_id": "s1", "draft_id": "upstream-draft",
                     "run_at": "", "timezone": "", "status": "SCHEDULED"},
        },
    })
    orig = mcp.execute_tool

    async def capture(tool_name: str, **kw: Any) -> dict[str, Any]:
        if tool_name == "publication.schedule":
            captured_schedule_args.update(kw)
        return await orig(tool_name, **kw)

    mcp.execute_tool = capture

    ctx = _ctx(
        mcp=mcp,
        requirements=[{"type": "CREATE"}, {"type": "PUBLISH"}],
    )
    service = RuntimeAgentService()
    result = await service.execute(ctx)

    assert result.success is True
    # The schedule call must have received the draft_id from upstream
    assert captured_schedule_args.get("draft_id") == "upstream-draft"


@pytest.mark.asyncio
async def test_create_then_modify_uses_one_task_context_without_reunderstanding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime consumes the already-resolved context on both turns."""
    from greenbook_assistant_core.task import understanding as understanding_module

    class ForbiddenTaskUnderstanding:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("Runtime must not create TaskUnderstanding")

    monkeypatch.setattr(
        understanding_module,
        "TaskUnderstanding",
        ForbiddenTaskUnderstanding,
    )

    create_intent = TaskIntent(
        relation="NEW_TASK",
        goal="AI Agent 学习路线",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    create_mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-a", "title": "AI Agent Guide"},
        },
    })
    create_ctx = RuntimeContext(
        run_id="run-create-context",
        trace_id="trace-create-context",
        user_id="u1",
        user_message="写一篇 AI Agent 学习路线",
        task_context=TaskContext(
            task_id="task-a",
            goal="AI Agent 学习路线",
            task_intent=create_intent,
        ),
        mcp=create_mcp,
    )
    service = RuntimeAgentService()
    created = await service.execute(create_ctx)
    assert created.success is True
    assert created.task_id == "task-a"

    revise_intent = TaskIntent(
        relation="MODIFY_TASK",
        goal="修改 AI Agent 学习路线",
        goal_category="IMPROVE_CONTENT",
        requirements=[{"type": "IMPROVE"}],
    )
    revise_mcp = _mock_mcp({
        "content.revise_draft": {
            "ok": True, "code": "",
            "data": {"draft_id": "draft-a", "title": "AI Agent Guide Revised"},
        },
    })
    revised = await service.execute(RuntimeContext(
        run_id="run-revise-context",
        trace_id="trace-revise-context",
        user_id="u1",
        user_message="修改一下内容",
        active_draft_id="draft-a",
        task_context=TaskContext(
            task_id="task-a",
            goal="AI Agent 学习路线",
            task_intent=revise_intent,
            active_artifact_id=(created.artifact_ids or [None])[-1],
            artifact_refs=created.artifacts,
        ),
        mcp=revise_mcp,
    ))

    assert revised.success is True
    assert revised.task_id == "task-a"


@pytest.mark.asyncio
async def test_schedule_uses_task_context_task_id() -> None:
    calls: list[str] = []
    mcp = _mock_mcp({
        "publication.schedule": {
            "ok": True, "code": "",
            "data": {
                "schedule_id": "schedule-a",
                "draft_id": "draft-a",
                "run_at": "2026-08-08T00:00:00Z",
                "timezone": "Asia/Shanghai",
                "status": "SCHEDULED",
            },
        },
    })
    original_execute = mcp.execute_tool

    async def capture(tool_name: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(tool_name)
        return await original_execute(tool_name, **kwargs)

    mcp.execute_tool = capture
    intent = TaskIntent(
        relation="CONTINUE_TASK",
        goal="AI Agent 学习路线",
        goal_category="PUBLISH_CONTENT",
        requirements=[{"type": "PUBLISH"}],
    )

    result = await RuntimeAgentService().execute(RuntimeContext(
        run_id="run-schedule-context",
        trace_id="trace-schedule-context",
        user_id="u1",
        user_message="五分钟之后发布",
        active_draft_id="draft-a",
        task_context=TaskContext(
            task_id="task-a",
            goal="AI Agent 学习路线",
            task_intent=intent,
            active_artifact_id="artifact-a",
            artifact_refs=[ArtifactRef(
                artifact_id="artifact-a",
                task_id="task-a",
                artifact_type="DRAFT",
                resource_id="draft-a",
                resource_kind="DRAFT",
            ).model_dump(mode="json")],
        ),
        mcp=mcp,
    ))

    assert result.success is True
    assert result.task_id == "task-a"
    assert "publication.schedule" in calls


@pytest.mark.asyncio
async def test_runtime_rejects_missing_task_context() -> None:
    result = await RuntimeAgentService().execute(RuntimeContext(
        run_id="run-missing-context",
        trace_id="trace-missing-context",
        user_id="u1",
        user_message="修改一下",
    ))

    assert result.success is False
    assert result.status == "FAILED"
    assert result.error_code == "TASK_CONTEXT_REQUIRED"


@pytest.mark.asyncio
async def test_queue_dispatch_creates_execution_without_running_tool() -> None:
    queue = ExecutionQueue()
    calls: list[str] = []
    mcp = _mock_mcp({
        "content.create_draft": {
            "ok": True,
            "code": "",
            "data": {"draft_id": "must-not-be-created"},
        },
    })

    async def execute_tool(tool_name: str, **_kwargs: Any) -> dict[str, Any]:
        calls.append(tool_name)
        return {"ok": True, "code": "", "data": {}}

    mcp.execute_tool = execute_tool
    service = RuntimeAgentService(
        execution_queue=queue,
        dispatch_mode="queue",
    )
    result = await service.execute(_ctx(mcp=mcp))

    assert result.status == "QUEUED"
    assert result.execution_id
    assert calls == []
    message = queue.get_by_execution_id(result.execution_id)
    assert message is not None
    assert message.status == ExecutionQueueStatus.READY
    assert "raw_access_token" not in message.payload
