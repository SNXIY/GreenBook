from __future__ import annotations

from app.agent_registry import agent_registry
from app.domain import CommunityIntent, IntentDelta, TargetBinding, TargetContext
from app.intent_delta_plan_compiler import IntentDeltaPlanCompiler
from app.execution import render_goal_delta_result
from app.plan_compiler import PlanCompiler
from app.tools import tool_registry


def _intent() -> CommunityIntent:
    return CommunityIntent(
        domain="content_edit",
        goal="Add Java code to the current post",
        required_capabilities=["generation", "draft_revision"],
        confidence=0.98,
    )


def _delta(*, preserve_schedule: bool) -> IntentDelta:
    return IntentDelta(
        delta_id="delta-1",
        goal_id="goal-1",
        run_id="run-1",
        message_id="message-1",
        operation="APPEND_CONTENT",
        target_ref="draft:draft-1",
        delta={"instruction": "Add executable Java examples"},
        preserve={"schedule": preserve_schedule},
        confidence=0.99,
    )


def _draft() -> TargetBinding:
    return TargetBinding(
        target_type="DRAFT",
        target_id="draft-1",
        artifact_id="artifact-draft-1",
        content_sha256="a" * 64,
        schedule_id="schedule-1",
        resolution_method="ACTIVE_TARGET",
    )


def test_content_delta_compiles_to_read_and_revise_without_creating_draft() -> None:
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=_delta(preserve_schedule=False),
        target_context=TargetContext(content_target=_draft()),
        intent=_intent(),
    )

    assert plan is not None
    assert [step.tool for step in plan.steps] == [
        "community.get_own_draft",
        "creator.revise_draft",
    ]
    assert all(step.tool != "creator.create_draft" for step in plan.steps)
    result = PlanCompiler(tools=tool_registry, agents=agent_registry).compile(plan)
    assert result.status == "EXECUTABLE"


def test_content_delta_preserves_schedule_by_rebinding_revised_artifact() -> None:
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=_delta(preserve_schedule=True),
        target_context=TargetContext(
            content_target=_draft(),
            schedule_target=TargetBinding(
                target_type="SCHEDULE",
                target_id="schedule-1",
                schedule_id="schedule-1",
                resolution_method="ACTIVE_TARGET",
            ),
        ),
        intent=_intent(),
    )

    assert plan is not None
    assert [step.tool for step in plan.steps] == [
        "community.get_own_draft",
        "publication.get_schedule",
        "creator.revise_draft",
        "publication.update_schedule",
    ]
    update = plan.steps[-1]
    assert set(update.depends_on) == {
        "read-current-schedule",
        "revise-current-draft",
    }
    result = PlanCompiler(tools=tool_registry, agents=agent_registry).compile(plan)
    assert result.status == "EXECUTABLE"
    assert result.compiled_plan is not None
    compiled_update = result.compiled_plan.steps[-1]
    assert compiled_update.artifact_sources == {
        "action_id": ["read-current-schedule"],
        "draft_id": ["revise-current-draft"],
        "expected_content_sha256": ["revise-current-draft"],
    }


def test_open_goal_still_uses_adaptive_planner() -> None:
    delta = _delta(preserve_schedule=False).model_copy(
        update={"operation": "CREATE_POST"}
    )
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=delta,
        target_context=TargetContext(content_target=_draft()),
        intent=_intent(),
    )
    assert plan is None


def test_goal_delta_response_comes_from_typed_receipts() -> None:
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=_delta(preserve_schedule=False),
        target_context=TargetContext(content_target=_draft()),
        intent=_intent(),
    )
    assert plan is not None

    response = render_goal_delta_result(
        plan,
        [
            {
                "tool": "creator.revise_draft",
                "result": {
                    "draft_id": "draft-1",
                    "title": "Agent learning guide",
                    "content_sha256": "b" * 64,
                },
            }
        ],
    )

    assert response is not None
    assert "draft-1" in response
    assert "Agent learning guide" in response


def test_cancel_schedule_delta_cannot_call_creator() -> None:
    delta = _delta(preserve_schedule=False).model_copy(
        update={"operation": "CANCEL_SCHEDULE", "preserve": {"content": True}}
    )
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=delta,
        target_context=TargetContext(
            content_target=_draft(),
            schedule_target=TargetBinding(
                target_type="SCHEDULE",
                target_id="schedule-1",
                schedule_id="schedule-1",
                resolution_method="ACTIVE_TARGET",
            ),
        ),
        intent=_intent(),
    )

    assert plan is not None
    assert [step.tool for step in plan.steps] == [
        "publication.get_schedule",
        "publication.cancel_schedule",
    ]
    assert all(not step.tool.startswith("creator.") for step in plan.steps)
    result = PlanCompiler(tools=tool_registry, agents=agent_registry).compile(plan)
    assert result.status == "EXECUTABLE"

    response = render_goal_delta_result(
        plan,
        [
            {
                "tool": "publication.cancel_schedule",
                "result": {"action_id": "schedule-1", "status": "CANCELLED"},
            }
        ],
    )
    assert response is not None
    assert "已取消" in response


def test_update_schedule_delta_compiles_without_model_planner() -> None:
    delta = _delta(preserve_schedule=False).model_copy(
        update={
            "operation": "UPDATE_SCHEDULE",
            "delta": {
                "message": "发布时间修改成五分钟之后",
                "schedule_request": "发布时间修改成五分钟之后",
            },
        }
    )
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=delta,
        target_context=TargetContext(
            content_target=_draft(),
            schedule_target=TargetBinding(
                target_type="SCHEDULE",
                target_id="schedule-1",
                schedule_id="schedule-1",
                resolution_method="ACTIVE_TARGET",
            ),
        ),
        intent=_intent(),
    )

    assert plan is not None
    assert [step.tool for step in plan.steps] == [
        "publication.get_schedule",
        "publication.update_schedule",
    ]
    assert "run_at" in plan.steps[-1].arguments
    result = PlanCompiler(tools=tool_registry, agents=agent_registry).compile(plan)
    assert result.status == "EXECUTABLE"


def test_publish_now_cancels_existing_schedule_before_publication() -> None:
    delta = _delta(preserve_schedule=False).model_copy(
        update={"operation": "PUBLISH_NOW"}
    )
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=delta,
        target_context=TargetContext(
            content_target=_draft(),
            schedule_target=TargetBinding(
                target_type="SCHEDULE",
                target_id="schedule-1",
                schedule_id="schedule-1",
                resolution_method="ACTIVE_TARGET",
            ),
        ),
        intent=_intent(),
    )

    assert plan is not None
    assert [step.tool for step in plan.steps] == [
        "publication.get_schedule",
        "publication.cancel_schedule",
        "community.get_own_draft",
        "publication.publish_now",
    ]
    assert set(plan.steps[-1].depends_on) == {
        "read-current-draft",
        "cancel-current-schedule",
    }
    result = PlanCompiler(tools=tool_registry, agents=agent_registry).compile(plan)
    assert result.status == "EXECUTABLE"
