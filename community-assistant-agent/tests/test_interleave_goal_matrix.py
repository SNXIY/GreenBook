"""End-to-end control-plane matrix for interleaved multi-goal dialogue.

Simulates: establish Goal A and B, then mutate A while B is the newer ACTIVE
goal. Asserts goal resolution + ChangeCompiler bind the correct targets.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.change_compiler import ChangeCompiler
from app.domain import (
    AdaptiveExecutionDecision,
    CommunityIntent,
    ConversationGoal,
    TargetBinding,
    TargetContext,
)
from app.turn_pipeline import TurnPipeline


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _goal(
    goal_id: str,
    title: str,
    *,
    draft_id: str,
    schedule_id: str,
    minutes_ago: int,
) -> ConversationGoal:
    content = TargetBinding(
        target_type="DRAFT",
        target_id=draft_id,
        content_sha256="a" * 64,
        schedule_id=schedule_id,
        resolution_method="ACTIVE_TARGET",
    )
    schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id=schedule_id,
        schedule_id=schedule_id,
        resolution_method="ACTIVE_TARGET",
    )
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conversation-interleave",
        intent="CONTENT_PUBLISH",
        summary=f"十分钟之后发布一条{title}的帖子",
        artifact_titles=[title],
        explicit_refs=[goal_id, f"goal:{goal_id}", draft_id, f"draft:{draft_id}", schedule_id],
        status="ACTIVE",
        phase="SCHEDULED",
        active_target_ref=f"draft:{draft_id}",
        target_context=TargetContext(
            content_target=content,
            schedule_target=schedule,
        ),
        updated_at=NOW - timedelta(minutes=minutes_ago),
    )


GOAL_A = _goal(
    "goal-a",
    "如何学习 MySQL",
    draft_id="draft-a",
    schedule_id="sched-a",
    minutes_ago=10,
)
GOAL_B = _goal(
    "goal-b",
    "Java 并发入门",
    draft_id="draft-b",
    schedule_id="sched-b",
    minutes_ago=1,
)


INTERLEAVE_ACTIONS = [
    ("改A时间", "把如何学习 MySQL 的发布时间改成十分钟之后", "UPDATE_SCHEDULE", "sched-a"),
    ("改A内容", "给如何学习 MySQL 增加实战经验", "APPEND_CONTENT", "draft-a"),
    (
        "改A内容加时间",
        "给如何学习 MySQL 增加代码，并且改成五分钟之后发布",
        "APPEND_CONTENT",
        "draft-a",
    ),
    ("取消A", "取消如何学习 MySQL 的定时发布", "CANCEL_SCHEDULE", "sched-a"),
    ("查A", "如何学习 MySQL 这篇什么时候发布？", "QUERY_SCHEDULE", "sched-a"),
]


@pytest.mark.parametrize(("name", "message", "expected_op", "expected_id"), INTERLEAVE_ACTIONS)
def test_interleaved_mutate_a_while_b_is_newer(
    name: str,
    message: str,
    expected_op: str,
    expected_id: str,
) -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary=name,
        intent=CommunityIntent(
            domain="content_publish",
            goal=message,
            required_capabilities=[],
            confidence=0.9,
        ),
        turn_relation="MODIFY",
    )
    pipeline = TurnPipeline()
    turn_intent, turn_plan, resolution = pipeline.interpret(
        message=message,
        decision=decision,
        conversation_goals=[GOAL_B, GOAL_A],
        has_established_goals=True,
        focus_goal_refs=["goal:goal-b", "goal:goal-a"],
    )
    assert resolution.outcome == "RESOLVED", name
    assert resolution.goal_id == "goal-a", name
    assert not turn_plan.open_plan

    result = pipeline.bind_and_compile(
        turn_plan=turn_plan,
        goal=GOAL_A,
        run_id="run-interleave",
        message_id="msg-interleave",
        intent=decision.intent,
        target_context=GOAL_A.target_context,
        client_timezone="Asia/Shanghai",
    )
    assert result.intent_delta is not None
    assert result.intent_delta.operation == expected_op
    assert result.compiled_plan is not None
    dumped = result.compiled_plan.model_dump_json()
    assert expected_id in dumped
    assert "draft-b" not in dumped
    assert "sched-b" not in dumped


def test_focus_stack_刚才那个_picks_top_focus_not_only_recency() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="switch",
        intent=CommunityIntent(
            domain="content_publish",
            goal="刚才那个任务发布时间改成十分钟之后",
            required_capabilities=[],
            confidence=0.9,
        ),
        turn_relation="MODIFY",
        primary_operation="UPDATE_SCHEDULE",
    )
    # Put A on top of focus stack even though B is newer.
    _, _, resolution = TurnPipeline().interpret(
        message="刚才那个任务发布时间改成十分钟之后",
        decision=decision,
        conversation_goals=[GOAL_B, GOAL_A],
        has_established_goals=True,
        focus_goal_refs=["goal:goal-a", "goal:goal-b"],
    )
    assert resolution.outcome == "RESOLVED"
    assert resolution.goal_id == "goal-a"


def test_router_hint_beats_conflicting_keywords() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="cancel",
        intent=CommunityIntent(
            domain="content_publish",
            goal="处理一下如何学习 MySQL",
            required_capabilities=[],
            confidence=0.9,
        ),
        turn_relation="MODIFY",
        primary_operation="CANCEL_SCHEDULE",
    )
    turn_intent, turn_plan, resolution = TurnPipeline().interpret(
        message="处理一下如何学习 MySQL",
        decision=decision,
        conversation_goals=[GOAL_B, GOAL_A],
        has_established_goals=True,
        focus_goal_refs=["goal:goal-a"],
    )
    assert resolution.goal_id == "goal-a"
    assert turn_intent.operation == "CANCEL_SCHEDULE"
    assert any(c.op == "CANCEL" for c in turn_plan.changes)


def test_new_analysis_stays_open_plan_with_existing_posts() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="analysis",
        intent=CommunityIntent(
            domain="data_analysis",
            goal="分析最近一周活跃用户",
            required_capabilities=["analysis"],
            confidence=0.9,
        ),
        turn_relation="NEW_GOAL",
        open_plan=True,
        primary_operation="OPEN_PLAN",
    )
    turn_intent, turn_plan, resolution = TurnPipeline().interpret(
        message="分析最近一周活跃用户",
        decision=decision,
        conversation_goals=[GOAL_B, GOAL_A],
        has_established_goals=True,
        focus_goal_refs=["goal:goal-b"],
    )
    assert resolution.outcome == "NEW_GOAL"
    assert turn_plan.open_plan
    assert turn_intent.operation == "OPEN_PLAN"
