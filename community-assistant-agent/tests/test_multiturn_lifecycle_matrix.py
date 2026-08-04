from __future__ import annotations

import pytest

from app.database import (
    ConversationGoal as ConversationGoalRecord,
    TargetBinding as TargetBindingRecord,
)
from app.domain import (
    CommunityIntent,
    ConversationGoal,
    TargetBinding,
    TargetContext,
)
from app.intent_delta import IntentDeltaParser
from app.intent_delta_plan_compiler import IntentDeltaPlanCompiler
from app.operation_contracts import OperationPlanGuard
from app.worker import AgentWorker


def _content() -> TargetBinding:
    return TargetBinding(
        target_type="DRAFT",
        target_id="draft-ops",
        content_sha256="a" * 64,
        schedule_id="schedule-ops",
        resolution_method="ACTIVE_TARGET",
    )


def _schedule() -> TargetBinding:
    return TargetBinding(
        target_type="SCHEDULE",
        target_id="schedule-ops",
        schedule_id="schedule-ops",
        resolution_method="ACTIVE_TARGET",
    )


def _goal() -> ConversationGoal:
    content = _content()
    return ConversationGoal(
        goal_id="goal-ops",
        conversation_id="conversation-ops",
        intent="CREATE_POST",
        phase="SCHEDULED",
        active_target_ref="draft:draft-ops",
        active_target=content,
        target_context=TargetContext(
            content_target=content,
            schedule_target=_schedule(),
        ),
    )


@pytest.mark.parametrize(
    ("message", "intent_goal", "wrong_plan_intent", "expected"),
    [
        (
            "把这个帖子的发布取消了吧",
            "取消当前定时发布任务",
            "APPEND_CONTENT",
            "CANCEL_SCHEDULE",
        ),
        (
            "撤销刚才的排期",
            "取消当前帖子的排期",
            "APPEND_CONTENT",
            "CANCEL_SCHEDULE",
        ),
        (
            "先别发布这个帖子",
            "停止当前定时发布",
            "APPEND_CONTENT",
            "CANCEL_SCHEDULE",
        ),
        (
            "发布时间改成五分钟之后",
            "修改当前定时发布时间",
            "APPEND_CONTENT",
            "UPDATE_SCHEDULE",
        ),
        (
            "立即发布这个帖子",
            "立即发布当前草稿",
            "APPEND_CONTENT",
            "PUBLISH_NOW",
        ),
        (
            "给这个帖子增加 Java 代码",
            "补充当前草稿内容",
            "CANCEL_SCHEDULE",
            "APPEND_CONTENT",
        ),
        (
            "修改一下这个帖子的标题",
            "更新当前草稿标题",
            "APPEND_CONTENT",
            "UPDATE_TITLE",
        ),
        (
            "重写正文并保留原排期",
            "重写当前草稿正文",
            "APPEND_CONTENT",
            "REPLACE_CONTENT",
        ),
    ],
)
def test_explicit_user_operation_wins_over_conflicting_plan_metadata(
    message: str,
    intent_goal: str,
    wrong_plan_intent: str,
    expected: str,
) -> None:
    goal = _goal()
    delta = IntentDeltaParser().parse(
        message=message,
        goal=goal,
        target_context=goal.target_context,
        run_id=f"run-{expected.lower()}",
        message_id=f"message-{expected.lower()}",
        turn_relation="MODIFY",
        intent_domain="content_publish",
        intent_goal=intent_goal,
        plan_intent=wrong_plan_intent,
    )
    assert delta.operation == expected

    plan = IntentDeltaPlanCompiler().compile(
        intent_delta=delta,
        target_context=goal.target_context,
        intent=CommunityIntent(
            domain="content_publish",
            goal=intent_goal,
            required_capabilities=[],
            confidence=0.98,
        ),
    )
    assert plan is not None
    guarded = OperationPlanGuard().enforce(intent_delta=delta, plan=plan)
    assert guarded.intent == expected


def test_persisted_goal_context_does_not_resurrect_cancelled_schedule_history() -> None:
    content = _content().model_copy(update={"schedule_id": None})
    goal = ConversationGoalRecord(
        id="goal-1",
        conversation_id="conversation-1",
        user_id="user-1",
        tenant_id="zhiguang",
        intent="CREATE_POST",
        status="ACTIVE",
        phase="READY",
        active_target_ref="draft:draft-ops",
        target_context=TargetContext(content_target=content).model_dump(mode="json"),
        version=1,
    )
    historical_schedule = TargetBindingRecord(
        id="binding-1",
        goal_id="goal-1",
        target_type="SCHEDULE",
        target_id="schedule-old",
        version=2,
        confidence=1.0,
        resolution_method="TOOL_OUTPUT",
        schedule_id="schedule-old",
    )

    context = AgentWorker._authoritative_target_context(
        goal,
        [historical_schedule],
    )

    assert context.content_target is not None
    assert context.schedule_target is None


def test_new_goal_rollover_only_happens_after_current_goal_has_business_state() -> None:
    empty = ConversationGoal(
        goal_id="goal-empty",
        conversation_id="conversation-1",
        intent="UNKNOWN",
        phase="DISCOVERING",
    )
    established = _goal()

    assert AgentWorker._goal_is_established(empty) is False
    assert AgentWorker._goal_is_established(established) is True
