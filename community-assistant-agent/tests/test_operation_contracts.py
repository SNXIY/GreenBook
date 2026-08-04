from __future__ import annotations

import pytest

from app.domain import AgentPlan, IntentDelta
from app.operation_contracts import OperationPlanGuard, OperationPlanViolation


def _delta(operation: str) -> IntentDelta:
    return IntentDelta(
        delta_id="delta-1",
        goal_id="goal-1",
        run_id="run-1",
        message_id="message-1",
        operation=operation,  # type: ignore[arg-type]
        delta={"message": "continue"},
    )


def _plan(intent: str, tools: list[str]) -> AgentPlan:
    return AgentPlan.model_validate(
        {
            "intent": intent,
            "summary": "candidate plan",
            "steps": [
                {
                    "task_id": f"step-{index}",
                    "tool": tool,
                    "label": tool,
                }
                for index, tool in enumerate(tools, start=1)
            ],
        }
    )


def test_intent_delta_overrides_only_the_plan_label_not_the_tool_contract() -> None:
    guarded = OperationPlanGuard().enforce(
        intent_delta=_delta("CANCEL_SCHEDULE"),
        plan=_plan(
            "APPEND_CONTENT",
            ["publication.get_schedule", "publication.cancel_schedule"],
        ),
    )
    assert guarded.intent == "CANCEL_SCHEDULE"


def test_cancel_schedule_rejects_creator_even_when_model_proposes_it() -> None:
    with pytest.raises(OperationPlanViolation, match="creator.revise_draft"):
        OperationPlanGuard().enforce(
            intent_delta=_delta("CANCEL_SCHEDULE"),
            plan=_plan(
                "APPEND_CONTENT",
                ["community.get_own_draft", "creator.revise_draft"],
            ),
        )


def test_content_edit_cannot_create_another_draft() -> None:
    with pytest.raises(OperationPlanViolation, match="creator.create_draft"):
        OperationPlanGuard().enforce(
            intent_delta=_delta("APPEND_CONTENT"),
            plan=_plan("CREATE_POST", ["creator.create_draft"]),
        )


def test_update_schedule_requires_the_update_side_effect() -> None:
    with pytest.raises(OperationPlanViolation, match="publication.update_schedule"):
        OperationPlanGuard().enforce(
            intent_delta=_delta("UPDATE_SCHEDULE"),
            plan=_plan("UPDATE_SCHEDULE", ["publication.get_schedule"]),
        )
