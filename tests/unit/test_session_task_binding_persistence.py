"""Regression tests for SessionContext task and artifact binding persistence."""

from __future__ import annotations

from greenbook_agent_core.context import SessionContext


def test_task_and_artifact_binding_round_trip() -> None:
    created = SessionContext(
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="tenant-1",
        active_task_id="task-1",
        active_artifact_id="artifact-1",
    )

    dumped = created.model_dump(mode="json")

    assert dumped["active_task_id"] == "task-1"
    assert dumped["active_artifact_id"] == "artifact-1"

    reloaded = SessionContext.model_validate(dumped)

    assert reloaded.active_task_id == "task-1"
    assert reloaded.active_artifact_id == "artifact-1"
