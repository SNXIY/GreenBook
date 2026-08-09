from __future__ import annotations

from app.artifact_contracts import ArtifactBinder
from app.domain import (
    CommunityIntent,
    ConversationGoal,
    ResolvedTargetView,
    TargetBinding,
    TargetContext,
)
from app.intent_delta import IntentDeltaParser
from app.intent_delta import TurnIntentParser
from app.intent_delta_plan_compiler import IntentDeltaPlanCompiler
from app.goal_resolver import GoalResolver
from app.operation_contracts import OperationPlanGuard
from app.tool_runtime import ToolAdapterRuntime, ToolRuntimeContext
from app.tools import tool_registry
from app.worker import AgentWorker


def _goal(*, goal_id: str, topic: str, schedule_id: str, version: int) -> ConversationGoal:
    content = TargetBinding(
        target_type="DRAFT",
        role="CONTENT",
        target_id=f"draft-{goal_id}",
        artifact_id=f"artifact-{goal_id}",
        content_sha256="a" * 64,
    )
    schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id=schedule_id,
        schedule_id=schedule_id,
        content_artifact_id=content.artifact_id,
        content_artifact_version=1,
    )
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conversation-multi-goal",
        intent=topic,
        summary=topic,
        phase="SCHEDULED",
        target_context=TargetContext(
            content_target=content,
            schedule_target=schedule,
        ),
        version=version,
    )


def test_query_goal_b_schedule_uses_read_view_without_binding_or_goal_mutation() -> None:
    goal_a = _goal(
        goal_id="goal-a",
        topic="包装实习简历帖子",
        schedule_id="schedule-a",
        version=4,
    )
    goal_b = _goal(
        goal_id="goal-b",
        topic="如何找实习帖子",
        schedule_id="schedule-b",
        version=7,
    )
    bindings = [
        goal_a.target_context.content_target,
        goal_a.target_context.schedule_target,
        goal_b.target_context.content_target,
        goal_b.target_context.schedule_target,
    ]
    binding_count = len(bindings)
    versions_before = {goal_a.goal_id: goal_a.version, goal_b.goal_id: goal_b.version}
    statuses_before = {goal_a.goal_id: goal_a.status, goal_b.goal_id: goal_b.status}
    contexts_before = {
        goal_a.goal_id: goal_a.target_context.model_dump(mode="json"),
        goal_b.goal_id: goal_b.target_context.model_dump(mode="json"),
    }

    turn_intent = TurnIntentParser().parse(
        message="查询找实习发布时间",
        has_target=True,
        turn_relation="QUERY_STATE",
        intent_domain="content_publish",
    )
    resolution = GoalResolver().resolve(
        turn_intent=turn_intent,
        goals=[goal_a, goal_b],
    )
    assert "goal_id" not in type(turn_intent).model_fields
    selected = {goal_a.goal_id: goal_a, goal_b.goal_id: goal_b}[
        str(resolution.goal_id)
    ]
    delta = IntentDeltaParser().bind(
        turn_intent=turn_intent,
        message="查询找实习发布时间",
        goal=selected,
        target_context=selected.target_context,
        run_id="run-query-goal-b",
        message_id="message-query-goal-b",
        turn_relation="QUERY_STATE",
        intent_domain="content_publish",
        intent_goal="查询如何找实习帖子的发布时间",
    )
    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=delta,
        target_context=selected.target_context,
        intent=CommunityIntent(
            domain="content_publish",
            goal="查询如何找实习帖子的发布时间",
            required_capabilities=[],
            risk="low",
            confidence=0.99,
        ),
    )

    assert delta.operation == "QUERY_SCHEDULE"
    assert delta.operation_class == "READ"
    assert resolution.outcome == "RESOLVED"
    assert resolution.goal_id == "goal-b"
    assert plan is not None
    plan = OperationPlanGuard().enforce(intent_delta=delta, plan=plan)
    assert [step.tool for step in plan.steps] == ["publication.get_schedule"]

    schedule_binding = selected.target_context.schedule_target
    assert schedule_binding is not None
    view = ResolvedTargetView.from_binding(
        goal_id=goal_b.goal_id,
        binding=schedule_binding,
    )
    arguments = ToolAdapterRuntime(ArtifactBinder()).prepare_arguments(
        definition=tool_registry.get("publication.get_schedule"),
        planner_arguments=plan.steps[0].arguments,
        artifacts=[],
        context=ToolRuntimeContext(
            prompt="查询找实习发布时间",
            context_post_id=None,
            context_comment_id=None,
            resolved_targets={"SCHEDULE": view},
        ),
    )
    tool_output = {
        "action_id": arguments["action_id"],
        "run_at": "2026-08-05T08:00:00+08:00",
        "status": "SCHEDULED",
    }

    assert arguments["action_id"] == "schedule-b"
    assert tool_output["run_at"] == "2026-08-05T08:00:00+08:00"
    assert view.goal_id == "goal-b"
    assert AgentWorker._allows_target_state_write(delta.operation_class) is False
    assert len(bindings) == binding_count
    assert {goal_a.goal_id: goal_a.version, goal_b.goal_id: goal_b.version} == versions_before
    assert {goal_a.goal_id: goal_a.status, goal_b.goal_id: goal_b.status} == statuses_before
    assert goal_a.target_context.model_dump(mode="json") == contexts_before[goal_a.goal_id]
    assert goal_b.target_context.model_dump(mode="json") == contexts_before[goal_b.goal_id]
