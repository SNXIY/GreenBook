"""Golden-path checks for the Runtime result presentation boundary."""

from __future__ import annotations

from apps.agent_api.greenbook_agent_api.models.runtime_result import RuntimeResult
from apps.agent_api.greenbook_agent_api.services.execution_presenter import (
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
                    "title": "Java learning route",
                    "content": "A practical Java learning route.",
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
    assert "Java learning route" in response.message
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


def test_failed_result_never_uses_success_copy() -> None:
    result = RuntimeResult(
        success=False,
        status="FAILED",
        execution_id="execution-failed-1",
        error_code="TOOL_ARGUMENT_VALIDATION_FAILED",
        error_message="content.create_draft is missing content",
        failure_state={"capability": "GENERATE_CONTENT"},
        content="Completed: publish tomorrow",
    )

    response = ExecutionResultPresenter().present(result)

    assert response.status == "FAILED"
    assert response.execution_id == "execution-failed-1"
    assert response.error_code == "TOOL_ARGUMENT_VALIDATION_FAILED"
