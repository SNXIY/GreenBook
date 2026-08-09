from __future__ import annotations

from apps.assistant_api.greenbook_assistant_api.models.runtime_result import RuntimeResult
from apps.assistant_api.greenbook_assistant_api.services.execution_projection_adapter import (
    ExecutionProjectionAdapter,
)


def test_running_generation_uses_product_progress_message() -> None:
    response = ExecutionProjectionAdapter().project(
        RuntimeResult(
            status="RUNNING",
            execution_id="execution-1",
            execution_path="runtime",
            steps=[
                {"capability": "GENERATE_CONTENT", "status": "RUNNING"},
                {"capability": "VALIDATE_QUALITY", "status": "PENDING"},
            ],
        )
    )

    assert "正在创作内容" in response.message
    assert "Execution execution-1" not in response.message


def test_failure_keeps_error_retry_and_artifact_projection() -> None:
    response = ExecutionProjectionAdapter().project(
        RuntimeResult(
            success=False,
            status="FAILED",
            execution_id="execution-2",
            error_code="TIMEOUT",
            error_message="Creator deadline exceeded",
            failure_state={
                "capability": "GENERATE_CONTENT",
                "status": "FAILED_RETRYABLE",
            },
            steps=[
                {
                    "capability": "GENERATE_CONTENT",
                    "status": "FAILED_RETRYABLE",
                    "error_message": "Creator deadline exceeded",
                }
            ],
            artifacts=[
                {
                    "type": "DRAFT",
                    "title": "Java 学习路线",
                    "content": "从基础到实战",
                    "resource_id": "draft-1",
                }
            ],
        )
    )

    assert response.retry_available is True
    assert "创作内容" in response.message
    assert response.artifacts[0].resource_id == "draft-1"
