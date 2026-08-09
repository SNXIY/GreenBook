"""Phase-2 control-plane: Task Bag, router hints, analysis/comment ops."""

from __future__ import annotations

from app.domain import AdaptiveExecutionDecision, CommunityIntent
from app.intent_delta import TurnIntentParser
from app.turn_plan import TurnPlanBuilder, split_task_bag_messages
from app.turn_pipeline import TurnPipeline


def test_split_task_bag_on_incidental_new_post() -> None:
    parts = split_task_bag_messages(
        "把如何学习 MySQL 的发布时间改成十分钟之后，顺便再写一篇 Redis 入门帖子"
    )
    assert len(parts) == 2
    assert "MySQL" in parts[0]
    assert "Redis" in parts[1]


def test_same_goal_compound_is_not_a_task_bag() -> None:
    parts = split_task_bag_messages(
        "给如何学习 MySQL 增加代码，并且改成五分钟之后发布"
    )
    assert parts == ["给如何学习 MySQL 增加代码，并且改成五分钟之后发布"]


def test_turn_plan_builder_nests_follow_up_tasks() -> None:
    plan = TurnPlanBuilder().build(
        message="把 MySQL 帖子改成十分钟后发布，顺便再写一篇 Java 并发帖子",
        turn_relation="MODIFY",
        intent_domain="content_publish",
        has_target=True,
    )
    assert plan.tasks
    assert plan.tasks[0].turn_relation == "NEW_GOAL"
    assert any(
        token in plan.tasks[0].raw_message for token in ("Java", "写一篇", "再写")
    )


def test_router_follow_up_prompts_override_heuristic() -> None:
    plan = TurnPlanBuilder().build(
        message="先改时间，再做别的",
        turn_relation="MODIFY",
        intent_domain="content_publish",
        has_target=True,
        follow_up_prompts=["写一篇 Postgres 调优帖子"],
    )
    assert len(plan.tasks) == 1
    assert "Postgres" in plan.tasks[0].raw_message


def test_router_primary_operation_hint_wins() -> None:
    plan = TurnPlanBuilder().build(
        message="处理一下刚才那个",
        turn_relation="MODIFY",
        intent_domain="content_publish",
        has_target=True,
        router_operation="CANCEL_SCHEDULE",
    )
    assert any(c.role == "SCHEDULE" and c.op == "CANCEL" for c in plan.changes)


def test_continue_analysis_is_open_plan_not_append() -> None:
    turn = TurnIntentParser().parse(
        message="继续深入分析活跃用户的发帖类型",
        has_target=True,
        turn_relation="CONTINUE",
        intent_domain="data_analysis",
    )
    assert turn.operation == "CONTINUE_ANALYSIS"
    plan = TurnPlanBuilder().from_turn_intent(
        turn_intent=turn,
        message=turn.raw_message or "继续深入分析活跃用户的发帖类型",
        turn_relation="CONTINUE",
        intent_domain="data_analysis",
    )
    assert plan.open_plan
    assert any(c.role == "ANALYSIS" for c in plan.changes)


def test_reply_comment_operation() -> None:
    turn = TurnIntentParser().parse(
        message="回复这条评论：谢谢支持",
        has_target=False,
        turn_relation="NEW_GOAL",
        intent_domain="comment_interaction",
    )
    assert turn.operation == "REPLY_COMMENT"


def test_pipeline_carries_router_open_plan() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="open analysis",
        intent=CommunityIntent(
            domain="data_analysis",
            goal="分析社区",
            required_capabilities=["analysis"],
            confidence=0.9,
        ),
        turn_relation="NEW_GOAL",
        open_plan=True,
        primary_operation="OPEN_PLAN",
    )
    _, turn_plan, resolution = TurnPipeline().interpret(
        message="分析最近一周社区活跃用户",
        decision=decision,
        conversation_goals=[],
        has_established_goals=False,
    )
    assert turn_plan.open_plan
    assert resolution.outcome == "NEW_GOAL"
