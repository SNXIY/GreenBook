"""Phase-3 TaskManager facade: lifecycle without target binding."""

from __future__ import annotations

from datetime import datetime, timezone

from app.domain import (
    AdaptiveExecutionDecision,
    CommunityIntent,
    ConversationGoal,
    TargetBinding,
    TargetContext,
)
from app.task_manager import TaskManager


def _goal(
    goal_id: str,
    summary: str,
    *,
    draft_id: str = "draft-java-1",
    schedule_id: str | None = "sched-java-1",
    phase: str = "SCHEDULED",
) -> ConversationGoal:
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conv-1",
        intent="CONTENT_PUBLISH",
        summary=summary,
        artifact_titles=[summary],
        status="ACTIVE",
        phase=phase,
        active_target_ref=f"draft:{draft_id}",
        target_context=TargetContext(
            content_target=TargetBinding(
                target_type="DRAFT",
                role="CONTENT",
                target_id=draft_id,
                artifact_id=f"artifact-{draft_id}",
            ),
            schedule_target=(
                TargetBinding(
                    target_type="SCHEDULE",
                    role="SCHEDULE",
                    target_id=schedule_id,
                )
                if schedule_id
                else None
            ),
        ),
        version=2,
        updated_at=datetime.now(timezone.utc),
    )


def _decision(
    *,
    turn_relation: str = "NEW_GOAL",
    goal: str = "写一篇Java帖子",
) -> AdaptiveExecutionDecision:
    return AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="test",
        intent=CommunityIntent(
            domain="content_publish",
            goal=goal,
            required_capabilities=["generation"],
            risk="low",
            confidence=0.9,
        ),
        turn_relation=turn_relation,  # type: ignore[arg-type]
    )


def test_create_java_post_is_create_action() -> None:
    manager = TaskManager()
    decision, turn = manager.prepare_action(
        message="写一篇Java帖子",
        decision=_decision(goal="写一篇Java帖子"),
        goals=[],
        focus_goal_refs=[],
    )
    assert turn.action == "CREATE"
    assert turn.goal_resolution is not None
    assert turn.goal_resolution.outcome == "NEW_GOAL"
    assert decision.turn_relation == "NEW_GOAL"


def test_update_does_not_bind_active_task() -> None:
    manager = TaskManager()
    active = _goal("goal-java", "Java入门指南")
    decision, turn = manager.prepare_action(
        message="修改内容，加入实战经验",
        decision=_decision(turn_relation="NEW_GOAL", goal="修改内容，加入实战经验"),
        goals=[active],
        focus_goal_refs=[f"goal:{active.goal_id}"],
    )
    assert turn.action == "UPDATE"
    assert turn.task is None
    assert turn.goal_resolution is None
    assert decision.turn_relation == "MODIFY"
    assert turn.force_has_target is True


def test_bind_resolved_target_after_resolver() -> None:
    manager = TaskManager()
    active = _goal("goal-java", "Java入门指南", schedule_id="sched-java-1")
    task = manager.resolve_active_task([active], [f"goal:{active.goal_id}"])
    assert task is not None
    bound = manager.bind_resolved_target(
        message="调整发布时间改成五分钟之后",
        action="UPDATE",
        task=task,
    )
    assert bound.action == "UPDATE"
    assert bound.task is not None
    assert bound.task.task_id == "goal-java"
    assert bound.goal_resolution is not None
    assert bound.goal_resolution.outcome == "RESOLVED"
    assert bound.operation_override == "UPDATE_SCHEDULE"


def test_resolve_target_delegate_selects_explicit_task() -> None:
    manager = TaskManager()
    java = _goal(
        "goal-java",
        "如何高效学好 Java：一份实用的学习路线图",
        draft_id="draft-java",
        schedule_id="sched-java",
    )
    fit = _goal(
        "goal-fit",
        "科学减肥：从饮食、运动到生活习惯的完整指南",
        draft_id="draft-fit",
        schedule_id="sched-fit",
    )
    result = manager.resolve_target(
        message="Java学习路线那篇修改发布时间",
        goals=[java, fit],
        focus_goal_refs=["goal:goal-java"],
    )
    assert result.resolution_method == "EXPLICIT_REFERENCE"
    assert result.task_id == "goal-java"
    assert result.schedule_id == "sched-java"
