from __future__ import annotations

from app.conversation_workspace import ConversationWorkspace, WorkspaceEntity
from app.domain import ConversationGoal, IntentDelta, TargetBinding, TargetContext
from app.target_resolver import TargetResolver


def _workspace(*labels: str) -> ConversationWorkspace:
    entities = [
        WorkspaceEntity(
            ref=f"draft:draft-{index}",
            kind="DRAFT",
            entity_id=f"draft-{index}",
            label=label,
            status="READY",
            source_run_id=f"run-{index}",
            source_artifact_id=f"artifact-{index}",
            actionable=True,
            created_at="2026-08-03T00:00:00+00:00",
        )
        for index, label in enumerate(labels, start=1)
    ]
    return ConversationWorkspace(
        conversation_id="conversation-1",
        focus_refs=[item.ref for item in entities],
        entities=entities,
        materialized_at="2026-08-03T00:00:00+00:00",
    )


def _delta() -> IntentDelta:
    return IntentDelta(
        delta_id="delta-1",
        goal_id="goal-1",
        run_id="run-2",
        message_id="message-2",
        operation="APPEND_CONTENT",
        target_ref="draft:draft-2",
    )


def _scheduled_content_target(schedule_id: str) -> TargetBinding:
    return TargetBinding(
        target_type="DRAFT",
        target_id="draft-redis",
        artifact_id="artifact-redis",
        schedule_id=schedule_id,
        resolution_method="TOOL_OUTPUT",
    )


def _goal(target: TargetBinding | None = None) -> ConversationGoal:
    return ConversationGoal(
        goal_id="goal-1",
        conversation_id="conversation-1",
        intent="CONTENT_PUBLISH",
        active_target_ref=(
            f"draft:{target.target_id}" if target is not None else None
        ),
        active_target=target,
    )


def _schedule_workspace() -> ConversationWorkspace:
    entities = [
        WorkspaceEntity(
            ref=f"schedule:schedule-{index}",
            kind="SCHEDULE",
            entity_id=f"schedule-{index}",
            label=f"定时任务 {index}",
            status="SCHEDULED",
            source_run_id=f"run-schedule-{index}",
            source_artifact_id=f"schedule-artifact-{index}",
            actionable=True,
            created_at="2026-08-03T00:00:00+00:00",
        )
        for index in (1, 2)
    ]
    return ConversationWorkspace(
        conversation_id="conversation-1",
        focus_refs=[item.ref for item in entities],
        entities=entities,
        materialized_at="2026-08-03T00:00:00+00:00",
    )


def test_single_target_is_bound_without_clarification() -> None:
    target = TargetBinding(
        target_type="DRAFT",
        target_id="draft-1",
        artifact_id="artifact-1",
        resolution_method="ACTIVE_TARGET",
    )
    result = TargetResolver().resolve(
        message="给它增加 Python 代码",
        intent_delta=_delta(),
        goal=_goal(target),
        workspace=_workspace("Redis 缓存三剑客"),
    )

    assert result.clarification is None
    assert result.selected is not None
    assert result.selected.target_id == "draft-1"


def test_similar_targets_trigger_clarification() -> None:
    target = TargetBinding(
        target_type="DRAFT",
        target_id="draft-2",
        artifact_id="artifact-2",
        resolution_method="ACTIVE_TARGET",
    )
    result = TargetResolver().resolve(
        message="给 Redis 帖子增加 Python 代码",
        intent_delta=_delta(),
        goal=_goal(target),
        workspace=_workspace("Redis 缓存三剑客", "Redis 高并发优化"),
    )

    assert result.clarification is not None
    assert len(result.clarification.candidates) == 2


def test_user_selection_resolves_and_restores_target() -> None:
    resolver = TargetResolver()
    clarification = resolver.resolve(
        message="给 Redis 帖子增加 Python 代码",
        intent_delta=_delta(),
        goal=_goal(
            TargetBinding(
                target_type="DRAFT",
                target_id="draft-2",
                artifact_id="artifact-2",
                resolution_method="ACTIVE_TARGET",
            )
        ),
        workspace=_workspace("Redis 缓存三剑客", "Redis 高并发优化"),
    ).clarification
    assert clarification is not None

    selected = resolver.resolve_selection(message="第一个", clarification=clarification)

    assert selected is not None
    assert selected.target_id == clarification.candidates[0].target_id


def test_user_can_resolve_clarification_by_candidate_title() -> None:
    resolver = TargetResolver()
    clarification = resolver.resolve(
        message="\u7ed9 Redis \u5e16\u5b50\u589e\u52a0 Python \u4ee3\u7801",
        intent_delta=_delta(),
        goal=_goal(
            TargetBinding(
                target_type="DRAFT",
                target_id="draft-2",
                artifact_id="artifact-2",
                resolution_method="ACTIVE_TARGET",
            )
        ),
        workspace=_workspace(
            "Redis \u7f13\u5b58\u4e09\u5251\u5ba2",
            "Redis \u9ad8\u5e76\u53d1\u4f18\u5316",
        ),
    ).clarification
    assert clarification is not None

    selected = resolver.resolve_selection(
        message="Redis \u7f13\u5b58\u4e09\u5251\u5ba2",
        clarification=clarification,
    )

    assert selected is not None
    assert selected.label == "Redis \u7f13\u5b58\u4e09\u5251\u5ba2"


def test_unknown_explicit_target_blocks_side_effect() -> None:
    result = TargetResolver().resolve(
        message="给 draft:not-owned 增加 Python 代码",
        intent_delta=_delta(),
        goal=_goal(
            TargetBinding(
                target_type="DRAFT",
                target_id="draft-1",
                artifact_id="artifact-1",
                resolution_method="ACTIVE_TARGET",
            )
        ),
        workspace=_workspace("Redis 缓存三剑客"),
    )

    assert result.error is not None
    assert result.selected is None
    assert result.clarification is None


def test_schedule_update_prefers_active_scheduled_draft_over_old_drafts() -> None:
    target = TargetBinding(
        target_type="DRAFT",
        target_id="draft-2",
        artifact_id="artifact-2",
        schedule_id="schedule-2",
        resolution_method="ACTIVE_TARGET",
    )
    result = TargetResolver().resolve(
        message="发布时间调整为五分钟之后",
        intent_delta=_delta().model_copy(update={"operation": "UPDATE_SCHEDULE"}),
        goal=_goal(target),
        workspace=_workspace("Redis 缓存三剑客", "Redis 高并发优化"),
        artifacts=[
            {
                "artifact_id": "schedule-artifact",
                "artifact_type": "SCHEDULE_RECEIPT",
                "content": {"action_id": "schedule-2", "draft_id": "draft-2"},
            }
        ],
    )

    assert result.clarification is None
    assert result.selected is not None
    assert result.selected.target_id == "schedule-2"


def test_schedule_and_content_targets_do_not_conflict() -> None:
    content = _scheduled_content_target("schedule-1")
    context = TargetContext(
        content_target=content,
        schedule_target=TargetBinding(
            target_type="SCHEDULE",
            target_id="schedule-1",
            artifact_id="schedule-artifact-1",
            schedule_id="schedule-1",
            resolution_method="TOOL_OUTPUT",
        ),
    )
    goal = _goal(content).model_copy(update={"target_context": context})
    result = TargetResolver().resolve(
        message="发布时间调整为五分钟之后",
        intent_delta=_delta().model_copy(update={"operation": "UPDATE_SCHEDULE"}),
        goal=goal,
        workspace=ConversationWorkspace(
            conversation_id="conversation-1",
            target_context=context,
            entities=[],
            materialized_at="2026-08-03T00:00:00+00:00",
        ),
    )

    assert result.clarification is None
    assert result.selected is not None
    assert result.selected.type == "SCHEDULE"
    assert result.selected.target_id == "schedule-1"


def test_target_context_routes_each_operation_to_its_role() -> None:
    context = TargetContext(
        content_target=TargetBinding(
            target_type="DRAFT",
            role="CONTENT",
            target_id="draft-1",
        ),
        schedule_target=TargetBinding(
            target_type="SCHEDULE",
            role="SCHEDULE",
            target_id="schedule-1",
        ),
    )

    assert context.for_operation("APPEND_CONTENT").target_id == "draft-1"
    assert context.for_operation("UPDATE_SCHEDULE").target_id == "schedule-1"


def test_resolver_does_not_use_legacy_active_target_as_business_context() -> None:
    goal = _goal(
        TargetBinding(
            target_type="DRAFT",
            role="CONTENT",
            target_id="draft-legacy",
        )
    )
    result = TargetResolver().resolve(
        message="鍙戝竷鏃堕棿璋冩暣涓轰簲鍒嗛挓涔嬪悗",
        intent_delta=_delta().model_copy(update={"operation": "UPDATE_SCHEDULE"}),
        goal=goal,
        workspace=ConversationWorkspace(
            conversation_id="conversation-1",
            entities=[],
            materialized_at="2026-08-03T00:00:00+00:00",
        ),
    )

    assert result.selected is None
    assert result.clarification is None


def test_two_schedules_trigger_same_type_clarification() -> None:
    context = TargetContext()
    goal = _goal()
    result = TargetResolver().resolve(
        message="发布时间调整为五分钟之后",
        intent_delta=_delta().model_copy(update={"operation": "UPDATE_SCHEDULE"}),
        goal=goal,
        workspace=ConversationWorkspace(
            conversation_id="conversation-1",
            target_context=context,
            entities=_schedule_workspace().entities,
            focus_refs=_schedule_workspace().focus_refs,
            materialized_at="2026-08-03T00:00:00+00:00",
        ),
    )

    assert result.clarification is not None
    assert {item.type for item in result.clarification.candidates} == {"SCHEDULE"}
    assert {item.target_id for item in result.clarification.candidates} == {
        "schedule-1",
        "schedule-2",
    }


def test_current_schedule_binding_wins_over_older_schedule_history() -> None:
    content = _scheduled_content_target("schedule-current")
    context = TargetContext(
        content_target=content,
        schedule_target=TargetBinding(
            target_type="SCHEDULE",
            target_id="schedule-current",
            artifact_id="schedule-artifact-current",
            schedule_id="schedule-current",
            resolution_method="TOOL_OUTPUT",
        ),
    )
    workspace = _schedule_workspace().model_copy(
        update={"target_context": context}
    )
    result = TargetResolver().resolve(
        message="\u53d1\u5e03\u65f6\u95f4\u4fee\u6539\u6210\u4e94\u5206\u949f\u4e4b\u540e",
        intent_delta=_delta().model_copy(update={"operation": "UPDATE_SCHEDULE"}),
        goal=_goal(content).model_copy(update={"target_context": context}),
        workspace=workspace,
    )

    assert result.clarification is None
    assert result.selected is not None
    assert result.selected.target_id == "schedule-current"
