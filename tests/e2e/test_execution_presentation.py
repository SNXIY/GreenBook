"""Golden-path checks for the Runtime result presentation boundary."""

from __future__ import annotations

import pytest

from apps.assistant_api.greenbook_assistant_api.models.runtime_result import RuntimeResult
from apps.assistant_api.greenbook_assistant_api.services.assistant_service import AssistantService
from apps.assistant_api.greenbook_assistant_api.services.execution_presenter import (
    ExecutionResultPresenter,
)


def _scheduled_result() -> RuntimeResult:
    return RuntimeResult(
        success=True,
        status="COMPLETED",
        execution_id="execution-publish-1",
        artifacts=[
            {
                "artifact_id": "artifact-draft-1",
                "artifact_type": "DRAFT",
                "resource_id": "draft-1",
                "data": {
                    "title": "Java 学习路线：从入门到实战",
                    "content": "先掌握语法基础，再通过项目练习建立完整知识体系。",
                },
            },
            {
                "artifact_id": "artifact-schedule-1",
                "artifact_type": "SCHEDULE",
                "resource_id": "schedule-1",
                "data": {
                    "run_at": "2026-08-10T00:00:00Z",
                    "timezone": "Asia/Shanghai",
                    "status": "SCHEDULED",
                },
            },
        ],
    )


def test_completed_publish_result_contains_artifact_and_schedule() -> None:
    response = ExecutionResultPresenter().present(_scheduled_result())

    assert response.status == "COMPLETED"
    assert response.execution_id == "execution-publish-1"
    assert "Java 学习路线" in response.message
    assert "内容摘要" in response.message
    assert "发布时间" in response.message
    assert "等待发布" in response.message
    assert {item.type for item in response.artifacts} == {
        "POST_DRAFT",
        "PUBLICATION_SCHEDULE",
    }


def test_waiting_approval_result_exposes_next_actions() -> None:
    result = RuntimeResult(
        success=False,
        status="WAITING_APPROVAL",
        execution_id="execution-approval-1",
        approval_id="approval-1",
        approval_data={"operation": "publication.publish_now"},
    )

    response = ExecutionResultPresenter().present(result)

    assert response.approval_required is True
    assert response.approval_id == "approval-1"
    assert response.next_actions == ["approve", "modify"]
    assert response.execution_id == "execution-approval-1"


def test_failed_result_never_uses_success_copy() -> None:
    result = RuntimeResult(
        success=False,
        status="FAILED",
        execution_id="execution-failed-1",
        error_code="TOOL_ARGUMENT_VALIDATION_FAILED",
        error_message="content.create_draft 缺少 content",
        failure_state={"capability": "GENERATE_CONTENT"},
        content="已完成：明天发布帖子",
    )

    response = ExecutionResultPresenter().present(result)

    assert response.status == "FAILED"
    assert response.execution_id == "execution-failed-1"
    assert "执行失败" in response.message
    assert "创作内容" in response.message
    assert "已完成" not in response.message
    assert response.error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"


@pytest.mark.asyncio
async def test_assistant_service_attaches_presentation_without_running_it() -> None:
    class StubRuntime:
        async def execute(self, _ctx):
            return _scheduled_result()

    service = AssistantService()
    service.register_runtime(StubRuntime())

    # RuntimeContext is intentionally duck-typed here: routing only needs a
    # user message and the service must not invoke any presentation side effect.
    class Context:
        user_message = "五分钟之后发布一篇 Java 帖子"
        run_id = "run-1"
        trace_id = "trace-1"

    result = await service.execute(Context())

    assert result.presentation is not None
    assert "Java 学习路线" in result.content
    assert result.presentation["execution_id"] == "execution-publish-1"
