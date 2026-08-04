from __future__ import annotations

from app.domain import ConversationGoal, TargetBinding, TargetContext
from app.intent_delta import IntentDeltaParser


def _goal(target_ref: str | None = None) -> ConversationGoal:
    return ConversationGoal(
        goal_id="goal-redis",
        conversation_id="conversation-1",
        intent="CONTENT_PUBLISH",
        phase="DRAFTING",
        active_target_ref=target_ref,
    )


def _draft_target(*, schedule_id: str | None = None) -> TargetBinding:
    return TargetBinding(
        target_type="DRAFT",
        role="CONTENT",
        target_id="draft-redis",
        artifact_id="artifact-redis",
        content_sha256="a" * 64,
        schedule_id=schedule_id,
        resolution_method="ACTIVE_TARGET",
    )


def _target_context(*, with_schedule: bool = False) -> TargetContext:
    schedule_id = "schedule-1" if with_schedule else None
    draft = _draft_target(schedule_id=schedule_id)
    ctx = TargetContext(content_target=draft)
    if with_schedule:
        ctx = ctx.model_copy(update={
            "schedule_target": TargetBinding(
                target_type="SCHEDULE",
                role="SCHEDULE",
                target_id="schedule-1",
                schedule_id="schedule-1",
            ),
        })
    return ctx


def test_create_then_append_stays_on_one_goal_and_target() -> None:
    parser = IntentDeltaParser()
    created = parser.parse(
        message="创建 Redis 缓存三剑客帖子，并五分钟后发布",
        goal=_goal(),
        target_context=TargetContext(),
        run_id="run-create",
        message_id="message-create",
        intent_domain="content_publish",
    )
    appended = parser.parse(
        message="给它增加 Java 和 Python 代码",
        goal=_goal("draft:draft-redis"),
        target_context=_target_context(with_schedule=True),
        run_id="run-append",
        message_id="message-append",
        turn_relation="MODIFY",
        intent_domain="content_publish",
    )

    assert created.operation == "CREATE_POST"
    assert appended.operation == "APPEND_CONTENT"
    assert appended.goal_id == created.goal_id
    assert appended.target_ref == "draft:draft-redis"
    assert appended.preserve["schedule"] is True


def test_new_relative_scheduled_post_ignores_stale_update_plan_label() -> None:
    message = "\u5341\u5206\u949f\u4e4b\u540e\u53d1\u5e03\u4e00\u6761\u5982\u4f55\u5b66\u4e60agent\u7684skill\u7684\u5e16\u5b50"
    goal = _goal()
    delta = IntentDeltaParser().parse(
        message=message,
        goal=goal,
        target_context=TargetContext(),
        run_id="run-new-relative-schedule",
        message_id="message-new-relative-schedule",
        turn_relation="NEW_GOAL",
        intent_domain="content_publish",
        intent_goal=message,
        plan_intent="UPDATE_SCHEDULE",
    )

    assert delta.operation == "CREATE_POST"
    assert delta.operation_class == "WRITE"
    assert delta.target_role is None


def test_create_then_update_schedule_preserves_the_draft() -> None:
    parser = IntentDeltaParser()
    delta = parser.parse(
        message="发布时间改成下午两点半",
        goal=_goal("draft:draft-redis"),
        target_context=_target_context(with_schedule=True),
        run_id="run-schedule",
        message_id="message-schedule",
        turn_relation="MODIFY",
        intent_domain="content_publish",
        plan_intent="UPDATE_SCHEDULE",
    )

    assert delta.operation == "UPDATE_SCHEDULE"
    assert delta.target_ref == "schedule:schedule-1"
    assert delta.target_role == "SCHEDULE"
    assert delta.preserve == {"content": True, "draft": True}


def test_chinese_schedule_variants_are_not_content_append() -> None:
    delta = IntentDeltaParser().parse(
        message="发布时间调整为五分钟之后",
        goal=_goal("draft:draft-redis"),
        target_context=_target_context(with_schedule=True),
        run_id="run-schedule-variant",
        message_id="message-schedule-variant",
        turn_relation="MODIFY",
        intent_domain="content_publish",
    )

    assert delta.operation == "UPDATE_SCHEDULE"


def test_publish_time_edit_is_not_misread_as_content_replacement() -> None:
    delta = IntentDeltaParser().parse(
        message="\u53d1\u5e03\u65f6\u95f4\u4fee\u6539\u4e00\u4e0b\uff0c\u4fee\u6539\u6210\u4e94\u5206\u949f\u4e4b\u540e",
        goal=_goal("draft:draft-redis"),
        target_context=_target_context(with_schedule=True),
        run_id="run-relative-schedule",
        message_id="message-relative-schedule",
        turn_relation="MODIFY",
        intent_domain="content_edit",
    )

    assert delta.operation == "UPDATE_SCHEDULE"


def test_create_then_publish_now_is_a_delta_not_a_new_draft() -> None:
    parser = IntentDeltaParser()
    delta = parser.parse(
        message="立即发布吧",
        goal=_goal("draft:draft-redis"),
        target_context=_target_context(with_schedule=True),
        run_id="run-publish",
        message_id="message-publish",
        turn_relation="CONTINUE",
        intent_domain="content_publish",
        plan_intent="PUBLISH_CONTINUATION_DRAFT",
    )

    assert delta.operation == "PUBLISH_NOW"
    assert delta.target_ref == "draft:draft-redis"
    assert delta.preserve["content"] is True


def test_cancel_publication_overrides_a_contradictory_content_edit_plan() -> None:
    delta = IntentDeltaParser().parse(
        message="把这个帖子的发布取消了吧",
        goal=_goal("draft:draft-redis"),
        target_context=_target_context(with_schedule=True),
        run_id="run-cancel",
        message_id="message-cancel",
        turn_relation="MODIFY",
        intent_domain="content_delete",
        intent_goal="取消当前定时发布任务",
        plan_intent="APPEND_CONTENT",
    )

    assert delta.operation == "CANCEL_SCHEDULE"
    assert delta.preserve == {"content": True, "schedule": False}


def test_lifecycle_queries_are_classified_as_read_operations() -> None:
    goal = _goal("draft:draft-redis").model_copy(
        update={
            "target_context": TargetContext(
                content_target=_draft_target(schedule_id="schedule-1"),
                schedule_target=TargetBinding(
                    target_type="SCHEDULE",
                    role="SCHEDULE",
                    target_id="schedule-1",
                    schedule_id="schedule-1",
                ),
            )
        }
    )
    parser = IntentDeltaParser()

    cases = [
        ("如何找实习这条帖子的发布时间是多少", "QUERY_SCHEDULE", "SCHEDULE"),
        ("这条帖子的内容是什么", "QUERY_CONTENT", "CONTENT"),
        ("这条帖子发布了吗", "QUERY_PUBLICATION_STATUS", "PUBLICATION"),
    ]
    for index, (message, operation, role) in enumerate(cases):
        delta = parser.parse(
            message=message,
            goal=goal,
            run_id=f"run-query-{index}",
            message_id=f"message-query-{index}",
            turn_relation="QUERY_STATE",
            intent_domain="content_publish",
        )

        assert delta.operation == operation
        assert delta.operation_class == "READ"
        assert delta.target_role == role
        assert delta.preserve == {}


def test_mutations_keep_write_and_side_effect_classes() -> None:
    parser = IntentDeltaParser()
    goal = _goal("draft:draft-redis")

    write = parser.parse(
        message="给帖子增加一段总结",
        goal=goal,
        target_context=_target_context(),
        run_id="run-write",
        message_id="message-write",
        turn_relation="MODIFY",
    )
    side_effect = parser.parse(
        message="立即发布",
        goal=goal,
        target_context=_target_context(),
        run_id="run-side-effect",
        message_id="message-side-effect",
        turn_relation="CONTINUE",
    )

    assert write.operation_class == "WRITE"
    assert side_effect.operation_class == "SIDE_EFFECT"
