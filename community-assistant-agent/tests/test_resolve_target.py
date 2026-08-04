"""Layer-A TargetResolver.resolve_target continuity tests (T1–T4)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.domain import ConversationGoal, TargetBinding, TargetContext
from app.target_resolver import TargetResolver


@dataclass(frozen=True)
class _Task:
    task_id: str
    artifact_id: str | None
    schedule_id: str | None
    summary: str | None


def _goal(
    goal_id: str,
    title: str,
    *,
    draft_id: str,
    schedule_id: str,
) -> ConversationGoal:
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conv-1",
        intent="CONTENT_PUBLISH",
        summary=title,
        artifact_titles=[title],
        status="ACTIVE",
        phase="SCHEDULED",
        active_target_ref=f"draft:{draft_id}",
        target_context=TargetContext(
            content_target=TargetBinding(
                target_type="DRAFT",
                role="CONTENT",
                target_id=draft_id,
                artifact_id=f"artifact-{draft_id}",
            ),
            schedule_target=TargetBinding(
                target_type="SCHEDULE",
                role="SCHEDULE",
                target_id=schedule_id,
            ),
        ),
        version=1,
    )


JAVA_TITLE = "如何高效学好 Java：一份实用的学习路线图"
FIT_TITLE = "科学减肥：从饮食、运动到生活习惯的完整指南"


def _fixture() -> tuple[list[ConversationGoal], list[_Task], _Task]:
    java = _goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
    ).model_copy(update={"updated_at": datetime.now(timezone.utc) - timedelta(minutes=10)})
    fit = _goal(
        "goal-fit",
        FIT_TITLE,
        draft_id="draft-fit",
        schedule_id="sched-fit",
    ).model_copy(update={"updated_at": datetime.now(timezone.utc) - timedelta(minutes=1)})
    tasks = [
        _Task("goal-java", "artifact-draft-java", "sched-java", JAVA_TITLE),
        _Task("goal-fit", "artifact-draft-fit", "sched-fit", FIT_TITLE),
    ]
    # active defaults to Java (most recent focus) for some tests; T2 overrides.
    return [java, fit], tasks, tasks[0]


def test_t1_explicit_title_selects_java_task() -> None:
    goals, tasks, active = _fixture()
    result = TargetResolver().resolve_target(
        message="Java学习路线那篇修改发布时间",
        active_task=active,
        active_tasks=tasks,
        goals=goals,
    )
    assert result.resolution_method == "EXPLICIT_REFERENCE"
    assert result.task_id == "goal-java"
    assert result.goal_id == "goal-java"
    assert result.artifact_id is not None
    assert result.schedule_id == "sched-java"
    assert all(
        {c.task_id, c.goal_id, c.artifact_id, c.schedule_id}
        for c in result.candidates
    )


def test_t2_temporal_reference_uses_active_task() -> None:
    goals, tasks, active = _fixture()
    # Active is fitness post; temporal deixis must bind active, not Java title.
    active = tasks[1]
    result = TargetResolver().resolve_target(
        message="修改刚才那个帖子",
        active_task=active,
        active_tasks=tasks,
        goals=goals,
    )
    assert result.resolution_method == "ACTIVE_TASK"
    assert result.task_id == "goal-fit"
    assert result.goal_id == "goal-fit"
    assert "TEMPORAL_REFERENCE" in result.reference_kinds


def test_t3_index_reference_selects_second_task() -> None:
    goals, tasks, active = _fixture()
    result = TargetResolver().resolve_target(
        message="第二个帖子改发布时间",
        active_task=active,
        active_tasks=tasks,
        goals=goals,
    )
    assert result.resolution_method == "INDEX_REFERENCE"
    assert result.task_id == "goal-fit"
    assert result.schedule_id == "sched-fit"
    assert "ORDINAL_REFERENCE" in result.reference_kinds


def test_t4_weak_direct_reference_is_ambiguous() -> None:
    goals, tasks, active = _fixture()
    result = TargetResolver().resolve_target(
        message="修改那个文章",
        active_task=active,
        active_tasks=tasks,
        goals=goals,
    )
    assert result.resolution_method == "AMBIGUOUS"
    assert result.task_id is None
    assert result.clarification is not None
    assert len(result.candidates) == 2


def test_t5_restart_recovers_active_from_persisted_goals() -> None:
    """ACTIVE_TASK must use rebuilt goals/focus — not process memory."""

    goals, tasks, _ = _fixture()
    # Simulate restart: new resolver instance, same durable goal snapshot.
    resolver = TargetResolver()
    rebuilt_active = tasks[0]
    result = resolver.resolve_target(
        message="修改刚才那个帖子",
        active_task=rebuilt_active,
        active_tasks=tasks,
        goals=goals,
    )
    assert result.resolution_method == "ACTIVE_TASK"
    assert result.task_id == rebuilt_active.task_id
    assert result.artifact_id is not None
    assert result.schedule_id is not None

    goals, tasks, _ = _fixture()
    rows = TargetResolver().build_entity_candidates(
        active_tasks=tasks,
        goals=goals,
    )
    assert len(rows) == 2
    java = next(item for item in rows if item.task_id == "goal-java")
    assert java.goal_id == "goal-java"
    assert java.artifact_id == "artifact-draft-java" or java.artifact_id == "draft-java"
    assert java.schedule_id == "sched-java"
    assert JAVA_TITLE in (java.labels or [])
