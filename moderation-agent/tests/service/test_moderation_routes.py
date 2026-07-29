from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from moderation.repositories import TaskStateConflictError
from moderation.schemas import (
    AgentDecision,
    EvidenceCollectionAudit,
    ModerationAction,
    ModerationTaskAccepted,
    ModerationTaskDetail,
    ModerationTaskStatus,
    RiskType,
)
from service import app


def task_detail(*, status: ModerationTaskStatus = ModerationTaskStatus.COMPLETED):
    now = datetime.now(UTC)
    task_id = uuid4()
    return ModerationTaskDetail(
        id=task_id,
        thread_id=str(uuid4()),
        status=status,
        content="A test content item",
        platform="default",
        agent_decision=AgentDecision(
            risk_type=RiskType.NORMAL,
            risk_score=0.05,
            confidence=0.9,
            recommended_action=ModerationAction.PASS,
            reason="No violation detected.",
            evidence_complete=True,
        ),
        final_action=(
            None if status == ModerationTaskStatus.WAITING_REVIEW else ModerationAction.PASS
        ),
        version=2,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def moderation_services():
    previous = getattr(app.state, "moderation_services", None)
    container = SimpleNamespace(
        workflow=SimpleNamespace(
            create_task=AsyncMock(),
            list_tasks=AsyncMock(return_value=[]),
            get_task=AsyncMock(),
            list_logs=AsyncMock(return_value=[]),
            submit_review=AsyncMock(),
        ),
        policies=SimpleNamespace(
            create=AsyncMock(),
            list=AsyncMock(return_value=[]),
        ),
        statistics=SimpleNamespace(get=AsyncMock()),
        community=SimpleNamespace(apply_task_result=AsyncMock()),
    )
    app.state.moderation_services = container
    yield container
    if previous is None:
        del app.state.moderation_services
    else:
        app.state.moderation_services = previous


def test_create_moderation_task(test_client, moderation_services) -> None:
    task = task_detail()
    moderation_services.workflow.create_task.return_value = ModerationTaskAccepted(
        task=task,
        requires_human_review=False,
    )

    response = test_client.post(
        "/moderation/tasks",
        json={"content": "A test content item", "platform": "default"},
    )

    assert response.status_code == 201
    assert response.json()["task"]["id"] == str(task.id)
    moderation_services.workflow.create_task.assert_awaited_once()


def test_create_moderation_task_async_accepted(
    test_client,
    moderation_services,
    monkeypatch,
) -> None:
    from core import settings

    monkeypatch.setattr(settings, "MODERATION_ASYNC_ENABLED", True)
    task = task_detail(status=ModerationTaskStatus.PENDING)
    task = task.model_copy(update={"agent_decision": None, "final_action": None, "version": 1})
    moderation_services.workflow.create_task.return_value = ModerationTaskAccepted(
        task=task,
        requires_human_review=False,
    )

    response = test_client.post(
        "/moderation/tasks",
        json={"content": "A test content item", "platform": "default"},
    )

    assert response.status_code == 202
    assert response.json()["task"]["status"] == "PENDING"
    moderation_services.workflow.create_task.assert_awaited_once()


def test_task_detail_exposes_structured_dynamic_evidence_audit(
    test_client,
    moderation_services,
) -> None:
    task = task_detail()
    assert task.agent_decision is not None
    task.agent_decision = task.agent_decision.model_copy(
        update={
            "evidence_collection": EvidenceCollectionAudit(
                complete=True,
                risk_hypotheses=[RiskType.NORMAL],
                called_tools=[],
                failed_tools=[],
                tool_call_count=0,
                tool_call_round=1,
                recommended_path="FAST_REVIEW",
                reason="No additional evidence was required.",
            )
        }
    )
    moderation_services.workflow.get_task.return_value = task

    response = test_client.get(f"/moderation/tasks/{task.id}")

    assert response.status_code == 200
    audit = response.json()["agent_decision"]["evidence_collection"]
    assert audit["dynamic_attempted"] is True
    assert audit["recommended_path"] == "FAST_REVIEW"
    assert audit["tool_call_round"] == 1


def test_list_pending_tasks_passes_filters(test_client, moderation_services) -> None:
    response = test_client.get(
        "/moderation/tasks",
        params={"status": "WAITING_REVIEW", "limit": 25, "offset": 5},
    )

    assert response.status_code == 200
    moderation_services.workflow.list_tasks.assert_awaited_once_with(
        status=ModerationTaskStatus.WAITING_REVIEW,
        limit=25,
        offset=5,
    )


def test_submit_review_maps_state_conflict_to_409(test_client, moderation_services) -> None:
    task_id = uuid4()
    moderation_services.workflow.submit_review.side_effect = TaskStateConflictError(
        "Task was already reviewed"
    )

    response = test_client.post(
        f"/moderation/tasks/{task_id}/review",
        json={
            "action": "PASS",
            "reviewer_id": "reviewer-1",
            "comment": "Approved",
            "expected_version": 2,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Task was already reviewed"


def test_moderation_routes_return_503_without_runtime(test_client) -> None:
    previous = getattr(app.state, "moderation_services", None)
    if previous is not None:
        del app.state.moderation_services
    try:
        response = test_client.get("/moderation/tasks")
    finally:
        if previous is not None:
            app.state.moderation_services = previous

    assert response.status_code == 503
