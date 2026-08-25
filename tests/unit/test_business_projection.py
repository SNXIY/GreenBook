"""Business projection matrix: canonical facts only, no Runtime enum leakage."""

from __future__ import annotations

import pytest

from greenbook_agent_core.execution.presenter import ExecutionResultPresenter
from greenbook_agent_core.execution.result_resolver import ResultResolver
from greenbook_agent_core.execution.runtime_result import RuntimeResult


def _project(result: RuntimeResult):
    projection = ExecutionResultPresenter().present(result).business_projection
    assert projection is not None
    return projection


@pytest.mark.parametrize(
    ("name", "result", "state"),
    [
        (
            "single draft",
            RuntimeResult(
                success=True,
                status="COMPLETED",
                artifacts=[{"type": "DRAFT", "resource_id": "d1", "status": "DRAFT", "title": "A"}],
            ),
            "DRAFT",
        ),
        (
            "scheduled",
            RuntimeResult(
                success=True,
                status="COMPLETED",
                artifacts=[
                    {"type": "DRAFT", "resource_id": "d1", "status": "DRAFT", "title": "A"},
                    {
                        "type": "SCHEDULE",
                        "resource_id": "s1",
                        "status": "SCHEDULED",
                        "run_at": "2026-08-22T05:00:00Z",
                        "timezone": "Asia/Shanghai",
                    },
                ],
            ),
            "SCHEDULED",
        ),
        (
            "immediate publish",
            RuntimeResult(
                success=True,
                status="COMPLETED",
                artifacts=[{"type": "PUBLISHED_POST", "resource_id": "p1", "status": "PUBLISHED", "title": "A"}],
            ),
            "PUBLISHED",
        ),
        (
            "cancel schedule retains draft",
            RuntimeResult(
                success=True,
                status="COMPLETED",
                artifacts=[
                    {"type": "DRAFT", "resource_id": "d1", "status": "DRAFT", "title": "A"},
                    {"type": "SCHEDULE", "resource_id": "s1", "status": "CANCELLED"},
                ],
            ),
            "CANCELLED",
        ),
        (
            "semantic confirmation",
            RuntimeResult(
                success=False,
                status="WAITING_SEMANTIC_CONFIRMATION",
                partial_results={"semantic_confirmation": {"objectives": [{"topic": "A"}]}},
            ),
            "NEEDS_CONFIRMATION",
        ),
        ("approval", RuntimeResult(success=False, status="WAITING_APPROVAL"), "NEEDS_APPROVAL"),
        (
            "result unknown",
            RuntimeResult(success=False, status="RESULT_UNKNOWN", error_code="RESULT_UNKNOWN"),
            "VERIFYING_RESULT",
        ),
        ("worker waiting", RuntimeResult(success=True, status="WAITING_EXTERNAL"), "PROCESSING"),
        (
            "write without verified business fact",
            RuntimeResult(
                success=True,
                status="COMPLETED",
                steps=[{"capability": "PUBLISH_NOW", "status": "COMPLETED"}],
            ),
            "VERIFYING_RESULT",
        ),
        (
            "business rejection",
            RuntimeResult(success=False, status="FAILED", error_code="BUSINESS_REJECTED"),
            "FAILED",
        ),
    ],
)
def test_single_business_state_mapping(name: str, result: RuntimeResult, state: str) -> None:
    assert _project(result).state == state, name


def test_multi_objective_success_and_failure_is_partial() -> None:
    projection = _project(RuntimeResult(
        success=False,
        status="PARTIAL_FAILURE",
        artifacts=[
            {"type": "POST", "resource_id": "p-a", "status": "PUBLISHED", "title": "A"},
            {"type": "SCHEDULE", "resource_id": "s-b", "status": "FAILED", "title": "B"},
        ],
        partial_results={"task_status": "PARTIAL_SUCCESS"},
    ))

    assert projection.state == "PARTIAL"
    assert projection.completed_count == 1
    assert projection.failed_count == 1


def test_superseded_work_is_history_only() -> None:
    projection = _project(RuntimeResult(
        success=False,
        status="SUPERSEDED",
        partial_results={"mutation_status": "SUPERSEDED"},
    ))

    assert projection.visible is False
    assert projection.state is None
    assert projection.actions == []


def test_successful_cancel_without_java_response_body_projects_cancelled() -> None:
    resolved = ResultResolver().resolve(RuntimeResult(
        success=True,
        status="COMPLETED",
        steps=[{"capability": "CANCEL_SCHEDULE", "status": "COMPLETED"}],
        artifacts=[
            {"type": "DRAFT", "resource_id": "d1", "status": "DRAFT", "title": "A"},
            {"type": "SCHEDULE", "resource_id": "s1"},
        ],
    ))
    response = ExecutionResultPresenter().present(resolved)

    assert response.business_projection is not None
    assert response.business_projection.state == "CANCELLED"
    assert "\u53d6\u6d88" in response.message
    assert "\u8349\u7a3f" in response.message
