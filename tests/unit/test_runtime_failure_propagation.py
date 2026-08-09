from __future__ import annotations

import pytest
from greenbook_assistant_api.models.runtime_context import RuntimeContext
from greenbook_assistant_api.services.runtime_agent_service import RuntimeAgentService
from greenbook_assistant_core.execution.repository import ExecutionRepository
from greenbook_assistant_core.task.models import TaskIntent


class _FailingMcp:
    async def execute_tool(self, tool_name: str, **kwargs: object) -> dict[str, object]:
        return {
            "ok": False,
            "code": "TOOL_ARGUMENT_VALIDATION_FAILED",
            "user_message": "工具参数校验失败",
            "retryable": False,
            "request_sent": False,
        }


@pytest.mark.asyncio
async def test_failed_step_is_not_wrapped_as_success() -> None:
    intent = TaskIntent(
        goal="写一篇Java学习文章",
        goal_category="CREATE_CONTENT",
        requirements=[{"type": "CREATE"}],
    )
    ctx = RuntimeContext(
        run_id="failure-run",
        trace_id="failure-trace",
        user_id="u1",
        tenant_id="t1",
        task_intent=intent,
        user_message=intent.goal,
        mcp=_FailingMcp(),
        session=None,
    )

    result = await RuntimeAgentService()._execute_single(ctx)

    assert result.success is False
    assert result.status == "FAILED"
    assert result.error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert result.execution_id
    execution = ExecutionRepository().find_by_id(result.execution_id)
    assert execution is not None
    assert execution.status.value == "FAILED"
    assert execution.steps[0].status.value == "FAILED"
    assert execution.steps[0].error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
    assert result.content.startswith("执行失败：")
    assert "已完成" not in result.content
