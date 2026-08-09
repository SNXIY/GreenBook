from __future__ import annotations

from app.domain import ConversationGoal, TurnIntent
from app.goal_resolver import GoalResolver


def _goal(
    goal_id: str,
    summary: str,
    *,
    aliases: list[str] | None = None,
    artifact_titles: list[str] | None = None,
    explicit_refs: list[str] | None = None,
) -> ConversationGoal:
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conversation-1",
        intent="CONTENT_PUBLISH",
        summary=summary,
        aliases=aliases or [],
        artifact_titles=artifact_titles or [],
        explicit_refs=explicit_refs or [],
    )


def _turn(subject: str, *, refs: list[str] | None = None) -> TurnIntent:
    return TurnIntent(
        operation="QUERY_SCHEDULE",
        operation_class="READ",
        target_role="SCHEDULE",
        semantic_subject=subject,
        explicit_refs=refs or [],
        confidence=0.97,
    )


def test_label_for_goal_distinguishes_same_title() -> None:
    from app.domain import TargetBinding, TargetContext

    published = _goal(
        "goal-a",
        "Agent 设计学习路径",
        artifact_titles=["Agent 设计学习路径：从概念到实践的实用指南"],
    ).model_copy(
        update={
            "phase": "PUBLISHED",
            "target_context": TargetContext(
                content_target=TargetBinding(
                    target_type="DRAFT",
                    role="CONTENT",
                    target_id="111",
                ),
                publication_target=TargetBinding(
                    target_type="POST",
                    role="PUBLICATION",
                    target_id="111",
                ),
            ),
        }
    )
    scheduled = _goal(
        "goal-b",
        "Agent 设计学习路径",
        artifact_titles=["Agent 设计学习路径：从概念到实践的实用指南"],
    ).model_copy(
        update={
            "phase": "SCHEDULED",
            "target_context": TargetContext(
                content_target=TargetBinding(
                    target_type="DRAFT",
                    role="CONTENT",
                    target_id="222",
                ),
                schedule_target=TargetBinding(
                    target_type="SCHEDULE",
                    role="SCHEDULE",
                    target_id="sched-222",
                ),
            ),
        }
    )
    left = GoalResolver.label_for_goal(published)
    right = GoalResolver.label_for_goal(scheduled)
    assert left != right
    assert "已发布" in left and "111" in left
    assert "已排定" in right and "222" in right


def test_explicit_id_has_priority_over_semantic_subject() -> None:
    goals = [
        _goal("goal-a", "包装实习简历", explicit_refs=["schedule-a"]),
        _goal("goal-b", "找实习", explicit_refs=["schedule-b"]),
    ]

    resolution = GoalResolver().resolve(
        turn_intent=_turn("包装实习简历", refs=["schedule-b"]),
        goals=goals,
    )

    assert resolution.outcome == "RESOLVED"
    assert resolution.goal_id == "goal-b"
    assert resolution.candidates[0].resolution_method == "EXPLICIT_ID"


def test_artifact_title_has_priority_over_goal_summary_similarity() -> None:
    goals = [
        _goal("goal-a", "实习经验", artifact_titles=["包装实习简历完整指南"]),
        _goal("goal-b", "包装求职材料"),
    ]

    resolution = GoalResolver().resolve(
        turn_intent=_turn("包装实习简历完整指南"),
        goals=goals,
    )

    assert resolution.outcome == "RESOLVED"
    assert resolution.goal_id == "goal-a"
    assert resolution.candidates[0].resolution_method in {"ARTIFACT_TITLE", "ARTIFACT_TITLE_EXACT"}


def test_ambiguous_subject_requires_clarification_without_mutating_goals() -> None:
    goals = [
        _goal("goal-a", "春招找实习"),
        _goal("goal-b", "暑期找实习"),
    ]
    versions = [goal.version for goal in goals]

    resolution = GoalResolver().resolve(
        turn_intent=_turn("找实习"),
        goals=goals,
    )

    assert resolution.outcome == "NEEDS_CLARIFICATION"
    assert [goal.version for goal in goals] == versions


def test_create_is_new_goal_and_missing_read_is_not_found() -> None:
    resolver = GoalResolver()
    create = TurnIntent(
        operation="CREATE_POST",
        operation_class="WRITE",
        semantic_subject="秋招准备",
        confidence=0.92,
    )

    assert resolver.resolve(turn_intent=create, goals=[]).outcome == "NEW_GOAL"
    assert resolver.resolve(turn_intent=_turn("不存在的主题"), goals=[]).outcome == "NOT_FOUND"
