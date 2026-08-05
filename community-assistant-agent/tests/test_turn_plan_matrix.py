"""Combinatorial matrix for multi-goal natural dialogue control plane.

These tests assert structured TurnPlan / ChangeCompiler outcomes — not
single example scripts — so interleaved goals stay correct under paraphrase.
"""

from __future__ import annotations

import pytest

from app.change_compiler import ChangeCompiler
from app.domain import (
    AdaptiveExecutionDecision,
    CommunityIntent,
    ConversationGoal,
    TargetBinding,
    TargetContext,
)
from app.goal_resolver import GoalResolver
from app.intent_delta import IntentDeltaParser, TurnIntentParser
from app.turn_plan import (
    TurnPlanBuilder,
    _has_time_expression,
    changes_from_operation,
    primary_operation_from_changes,
)
from app.turn_pipeline import TurnPipeline


def _binding(draft_id: str, schedule_id: str | None = None) -> TargetBinding:
    return TargetBinding(
        target_type="DRAFT",
        target_id=draft_id,
        content_sha256="a" * 64,
        schedule_id=schedule_id,
        resolution_method="ACTIVE_TARGET",
    )


def _goal(
    goal_id: str,
    title: str,
    *,
    draft_id: str,
    schedule_id: str,
) -> ConversationGoal:
    content = _binding(draft_id, schedule_id)
    schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id=schedule_id,
        schedule_id=schedule_id,
        resolution_method="ACTIVE_TARGET",
    )
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conversation-matrix",
        intent="CONTENT_PUBLISH",
        summary=f"十分钟之后发布一条{title}的帖子",
        artifact_titles=[title],
        explicit_refs=[goal_id, f"goal:{goal_id}", draft_id, schedule_id],
        phase="SCHEDULED",
        active_target_ref=f"draft:{draft_id}",
        target_context=TargetContext(
            content_target=content,
            schedule_target=schedule,
        ),
    )


GOAL_A = _goal("goal-a", "如何学习 MySQL", draft_id="draft-a", schedule_id="sched-a")
GOAL_B = _goal("goal-b", "Java 并发入门", draft_id="draft-b", schedule_id="sched-b")


ACTIONS = [
    (
        "改时间",
        "把如何学习 MySQL 的发布时间改成十分钟之后",
        [("SCHEDULE", "UPDATE")],
        "UPDATE_SCHEDULE",
    ),
    (
        "改内容",
        "给如何学习 MySQL 这篇增加一些实战经验",
        [("CONTENT", "APPEND")],
        "APPEND_CONTENT",
    ),
    (
        "改内容加时间",
        "给如何学习 MySQL 增加代码示例，并且改成五分钟之后发布",
        [("CONTENT", "APPEND"), ("SCHEDULE", "UPDATE")],
        "APPEND_CONTENT",
    ),
    (
        "取消",
        "取消如何学习 MySQL 的定时发布",
        [("SCHEDULE", "CANCEL")],
        "CANCEL_SCHEDULE",
    ),
    (
        "查询",
        "如何学习 MySQL 这篇什么时候发布？",
        [("SCHEDULE", "QUERY")],
        "QUERY_SCHEDULE",
    ),
    (
        "立即发布",
        "立即发布如何学习 MySQL 这篇帖子",
        [("PUBLICATION", "PUBLISH_NOW")],
        "PUBLISH_NOW",
    ),
]


@pytest.mark.parametrize(
    ("_name", "message", "expected_roles", "expected_op"),
    ACTIONS,
)
def test_action_matrix_builds_composable_changes(
    _name: str,
    message: str,
    expected_roles: list[tuple[str, str]],
    expected_op: str,
) -> None:
    turn_intent = TurnIntentParser().parse(
        message=message,
        has_target=True,
        turn_relation="MODIFY",
        intent_domain="content_publish",
        intent_goal=message,
    )
    plan = TurnPlanBuilder().from_turn_intent(
        turn_intent=turn_intent,
        message=message,
        turn_relation="MODIFY",
        intent_domain="content_publish",
    )
    assert [(c.role, c.op) for c in plan.changes] == expected_roles
    assert primary_operation_from_changes(plan.changes, open_plan=False) == expected_op
    assert not plan.open_plan


@pytest.mark.parametrize(
    ("_name", "message", "_roles", "_op"),
    ACTIONS,
)
def test_action_matrix_resolves_goal_a_not_latest_goal_b(
    _name: str,
    message: str,
    _roles: list[tuple[str, str]],
    _op: str,
) -> None:
    turn_intent = TurnIntentParser().parse(
        message=message,
        has_target=True,
        turn_relation="MODIFY",
        intent_domain="content_publish",
        intent_goal=message,
    )
    # Goal B is newer; A must still win via title match.
    resolution = GoalResolver().resolve(
        turn_intent=turn_intent,
        goals=[GOAL_B, GOAL_A],
        raw_message=message,
    )
    assert resolution.outcome == "RESOLVED"
    assert resolution.goal_id == "goal-a"


@pytest.mark.parametrize(
    ("_name", "message", "_roles", "expected_op"),
    ACTIONS,
)
def test_change_compiler_binds_goal_a_targets(
    _name: str,
    message: str,
    _roles: list[tuple[str, str]],
    expected_op: str,
) -> None:
    turn_intent = TurnIntentParser().parse(
        message=message,
        has_target=True,
        turn_relation="MODIFY",
        intent_domain="content_publish",
        intent_goal=message,
    )
    turn_plan = TurnPlanBuilder().from_turn_intent(
        turn_intent=turn_intent,
        message=message,
        turn_relation="MODIFY",
        intent_domain="content_publish",
    )
    intent = CommunityIntent(
        domain="content_publish",
        goal=message,
        required_capabilities=[],
        confidence=0.95,
    )
    plan = ChangeCompiler().compile(
        turn_plan=turn_plan,
        target_context=GOAL_A.target_context,
        intent=intent,
        client_timezone="Asia/Shanghai",
    )
    # QUERY / schedule update / cancel / publish / content(+schedule) must compile
    # against Goal A targets — never invent Goal B ids.
    assert plan is not None
    rendered = plan.model_dump_json()
    assert "draft-a" in rendered or "sched-a" in rendered
    assert "draft-b" not in rendered
    assert "sched-b" not in rendered
    assert plan.intent == expected_op or (
        expected_op == "APPEND_CONTENT" and plan.intent == "APPEND_CONTENT"
    )


def test_open_plan_for_analysis_does_not_become_create_post() -> None:
    message = "分析最近一周社区最活跃的三个用户"
    turn_intent = TurnIntentParser().parse(
        message=message,
        has_target=True,
        turn_relation="NEW_GOAL",
        intent_domain="data_analysis",
        intent_goal=message,
    )
    assert turn_intent.operation == "OPEN_PLAN"
    plan = TurnPlanBuilder().from_turn_intent(
        turn_intent=turn_intent,
        message=message,
        turn_relation="NEW_GOAL",
        intent_domain="data_analysis",
    )
    assert plan.open_plan
    assert plan.changes == []


def test_content_plus_schedule_compound_compiles_rebind_with_new_time() -> None:
    message = "给如何学习 MySQL 增加代码，并且改成五分钟之后发布"
    turn_intent = TurnIntentParser().parse(
        message=message,
        has_target=True,
        turn_relation="MODIFY",
        intent_domain="content_publish",
    )
    turn_plan = TurnPlanBuilder().from_turn_intent(
        turn_intent=turn_intent,
        message=message,
        turn_relation="MODIFY",
        intent_domain="content_publish",
    )
    assert any(c.role == "CONTENT" for c in turn_plan.changes)
    assert any(c.role == "SCHEDULE" for c in turn_plan.changes)
    compiled = ChangeCompiler().compile(
        turn_plan=turn_plan,
        target_context=GOAL_A.target_context,
        intent=CommunityIntent(
            domain="content_publish",
            goal=message,
            required_capabilities=[],
            confidence=0.9,
        ),
        client_timezone="Asia/Shanghai",
    )
    assert compiled is not None
    tools = [step.tool for step in compiled.steps]
    assert "creator.revise_draft" in tools
    assert "publication.update_schedule" in tools
    rebind = next(
        step for step in compiled.steps if step.task_id == "rebind-current-schedule"
    )
    assert rebind.arguments.get("run_at")


def test_turn_pipeline_new_goal_when_subject_is_new_post() -> None:
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="new post",
        intent=CommunityIntent(
            domain="content_publish",
            goal="写一篇 Redis 入门帖子",
            required_capabilities=["generation"],
            confidence=0.9,
        ),
        turn_relation="NEW_GOAL",
    )
    pipeline = TurnPipeline()
    turn_intent, turn_plan, resolution = pipeline.interpret(
        message="写一篇 Redis 入门帖子",
        decision=decision,
        conversation_goals=[GOAL_A, GOAL_B],
        has_established_goals=True,
    )
    assert turn_intent.operation == "CREATE_POST"
    assert resolution.outcome == "NEW_GOAL"
    assert turn_plan.changes[0].op == "CREATE"


def test_paraphrase_schedule_update_stable() -> None:
    variants = [
        "把如何学习 MySQL 的发布时间改成十分钟之后",
        "如何学习 MySQL 这篇推迟到十分钟后发布",
        "调整一下如何学习 MySQL 的定时时间，十分钟之后",
    ]
    ops = []
    for message in variants:
        op = IntentDeltaParser._operation(
            text=message,
            has_target=True,
            turn_relation="MODIFY",
            plan_intent=None,
            intent_domain="content_publish",
            intent_goal=message,
        )
        ops.append(op)
    assert set(ops) == {"UPDATE_SCHEDULE"}


def test_changes_from_operation_compound_expand() -> None:
    changes = changes_from_operation(
        "APPEND_CONTENT",
        message="改内容并五分钟后发布",
        schedule_request="五分钟之后发布",
    )
    assert [c.role for c in changes] == ["CONTENT", "SCHEDULE"]


def test_router_update_schedule_does_not_drop_content_half() -> None:
    """Live miss: router labeled UPDATE_SCHEDULE and content edit was skipped."""

    message = (
        "《如何高效学好 Java：一份可执行的路线图》修改这个帖子的内容，"
        "加入一些实战经验，然后发布时间改成五分钟之后"
    )
    plan = TurnPlanBuilder().build(
        message=message,
        turn_relation="MODIFY",
        intent_domain="content_edit",
        has_target=True,
        router_operation="UPDATE_SCHEDULE",
        prefer_router=True,
    )
    assert [(c.role, c.op) for c in plan.changes] == [
        ("CONTENT", "APPEND"),
        ("SCHEDULE", "UPDATE"),
    ]
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="reschedule",
        intent=CommunityIntent(
            domain="content_edit",
            goal=message,
            required_capabilities=["schedule_publish"],
            confidence=0.9,
        ),
        turn_relation="MODIFY",
        primary_operation="UPDATE_SCHEDULE",
        open_plan=False,
    )
    goal_a = _goal(
        "goal-java",
        "如何高效学好 Java：一份可执行的路线图",
        draft_id="draft-java",
        schedule_id="sched-java",
    )
    goal_b = _goal(
        "goal-pcb",
        "PCB设计入门指南",
        draft_id="draft-pcb",
        schedule_id="sched-pcb",
    )
    turn_intent, turn_plan, resolution = TurnPipeline().interpret(
        message=message,
        decision=decision,
        conversation_goals=[goal_b, goal_a],
        has_established_goals=True,
        focus_goal_refs=[f"goal:{goal_b.goal_id}"],
    )
    assert turn_intent.operation == "APPEND_CONTENT"
    assert resolution.goal_id == "goal-java"
    assert [(c.role, c.op) for c in turn_plan.changes] == [
        ("CONTENT", "APPEND"),
        ("SCHEDULE", "UPDATE"),
    ]
    compiled = ChangeCompiler().compile(
        turn_plan=turn_plan,
        target_context=goal_a.target_context,
        intent=decision.intent,
        client_timezone="Asia/Shanghai",
    )
    assert compiled is not None
    assert "creator.revise_draft" in [step.tool for step in compiled.steps]
    assert "publication.update_schedule" in [step.tool for step in compiled.steps]


def test_live_java_title_compound_not_open_plan_or_schedule_only() -> None:
    """Regression: interleaved Goal A mutate after Goal B was created.

    Live failure used title quote + content edit + reschedule, then fell into
    OPEN_PLAN / schedule_update capability gap repair loops.
    """

    message = (
        "《如何高效学好 Java：一份可执行的路线图》修改这个帖子的内容，"
        "加入一些实战经验，然后发布时间改成五分钟之后"
    )
    goal_a = _goal(
        "goal-java",
        "如何高效学好 Java：一份可执行的路线图",
        draft_id="342915500847796224",
        schedule_id="6f2d1e07-7bea-4dee-ab6e-c821d10f575d",
    )
    goal_b = _goal(
        "goal-pcb",
        "从零开始学电路板设计",
        draft_id="342915745048563712",
        schedule_id="771a822e-158d-4c16-87c7-bba7d6655159",
    )
    assert (
        IntentDeltaParser._operation(
            text=message,
            has_target=True,
            turn_relation="MODIFY",
            plan_intent="OPEN_PLAN",
            intent_domain="content_edit",
            intent_goal=message,
        )
        == "APPEND_CONTENT"
    )
    plan = TurnPlanBuilder().build(
        message=message,
        turn_relation="MODIFY",
        intent_domain="content_edit",
        has_target=True,
        router_operation="OPEN_PLAN",
        router_open_plan=True,
        prefer_router=True,
    )
    assert not plan.open_plan
    assert [(c.role, c.op) for c in plan.changes] == [
        ("CONTENT", "APPEND"),
        ("SCHEDULE", "UPDATE"),
    ]
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="open-ish router",
        intent=CommunityIntent(
            domain="content_edit",
            goal=message,
            required_capabilities=["rewrite_content", "schedule_update"],
            confidence=0.9,
        ),
        turn_relation="MODIFY",
        primary_operation="OPEN_PLAN",
        open_plan=True,
    )
    turn_intent, turn_plan, resolution = TurnPipeline().interpret(
        message=message,
        decision=decision,
        conversation_goals=[goal_b, goal_a],
        has_established_goals=True,
        focus_goal_refs=[f"goal:{goal_b.goal_id}"],
    )
    assert turn_intent.operation == "APPEND_CONTENT"
    assert resolution.outcome == "RESOLVED"
    assert resolution.goal_id == "goal-java"
    compiled = ChangeCompiler().compile(
        turn_plan=turn_plan,
        target_context=goal_a.target_context,
        intent=decision.intent,
        client_timezone="Asia/Shanghai",
    )
    assert compiled is not None
    tools = [step.tool for step in compiled.steps]
    assert "creator.revise_draft" in tools
    assert "publication.update_schedule" in tools
    assert "schedule_publish" in (compiled.intent_detail.required_capabilities or [])
    assert "schedule_update" not in (compiled.intent_detail.required_capabilities or [])


@pytest.mark.parametrize(
    "message",
    [
        "Create a draft and schedule it in five minutes.",
        "Create a draft and publish it five minutes from now.",
        "Create a draft for tomorrow.",
    ],
)
def test_create_schedule_recognizes_english_relative_time(message: str) -> None:
    assert _has_time_expression(message)
