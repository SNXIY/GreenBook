"""TaskDelta normalization: one delta spanning multiple business resources is
decomposed into one mutation per resource at the command boundary.

Regression: "Java 集合那篇标题改得更吸引人一点，发布时间改成明天下午4点" was
bundled into ONE UPDATE_DRAFT delta carrying both ``title`` and ``run_at``, so
the schedule change never reached UPDATE_SCHEDULE.
"""

from __future__ import annotations

from greenbook_agent_core.command.models import TaskDelta, TaskDeltaOperation
from greenbook_agent_core.command.normalization import normalize_task_deltas


def _delta(operation: TaskDeltaOperation, **desired) -> TaskDelta:
    return TaskDelta(
        operation=operation,
        target_reference={"label": "Java 集合", "kind": "TASK"},
        desired_changes=dict(desired),
    )


def _actions(deltas: list[TaskDelta]) -> list[str]:
    return [
        str(delta.desired_changes.get("semantic_action") or "")
        for delta in deltas
    ]


def test_title_and_run_at_split_into_draft_and_schedule() -> None:
    deltas = normalize_task_deltas([
        _delta(
            TaskDeltaOperation.UPDATE_GOAL,
            semantic_action="UPDATE_DRAFT",
            title="更吸引人的标题",
            run_at="明天下午4点",
            temporal_base="EXPLICIT_DATETIME",
        )
    ])
    assert len(deltas) == 2
    assert _actions(deltas) == ["UPDATE_DRAFT", "UPDATE_SCHEDULE"]
    assert deltas[0].desired_changes == {
        "semantic_action": "UPDATE_DRAFT",
        "title": "更吸引人的标题",
    }
    assert deltas[1].desired_changes == {
        "semantic_action": "UPDATE_SCHEDULE",
        "run_at": "明天下午4点",
        "temporal_base": "EXPLICIT_DATETIME",
    }
    # Same owning reference on both.
    assert deltas[0].target_reference == deltas[1].target_reference
    # Distinct change ids so a deduplicating consumer does not drop the sibling.
    assert deltas[0].change_id != deltas[1].change_id
    assert deltas[0].operation == TaskDeltaOperation.UPDATE_GOAL
    assert deltas[1].operation == TaskDeltaOperation.UPDATE_GOAL


def test_title_only_stays_single_update_draft() -> None:
    deltas = normalize_task_deltas([
        _delta(
            TaskDeltaOperation.UPDATE_GOAL,
            semantic_action="UPDATE_DRAFT",
            title="新标题",
        )
    ])
    assert len(deltas) == 1
    assert deltas[0].desired_changes.get("semantic_action") == "UPDATE_DRAFT"
    assert deltas[0].desired_changes.get("title") == "新标题"


def test_run_at_only_stays_update_schedule() -> None:
    deltas = normalize_task_deltas([
        _delta(
            TaskDeltaOperation.UPDATE_GOAL,
            semantic_action="UPDATE_SCHEDULE",
            run_at="明天下午4点",
        )
    ])
    assert len(deltas) == 1
    assert deltas[0].desired_changes.get("semantic_action") == "UPDATE_SCHEDULE"
    assert deltas[0].desired_changes.get("run_at") == "明天下午4点"


def test_mislabeled_draft_delta_with_only_run_at_becomes_schedule() -> None:
    # Model said UPDATE_DRAFT but only carried a schedule field; the schedule
    # mutation must win instead of being silently dropped.
    deltas = normalize_task_deltas([
        _delta(
            TaskDeltaOperation.UPDATE_GOAL,
            semantic_action="UPDATE_DRAFT",
            run_at="明天下午4点",
        )
    ])
    assert len(deltas) == 1
    assert deltas[0].desired_changes.get("semantic_action") == "UPDATE_SCHEDULE"


def test_title_and_content_stay_one_update_draft() -> None:
    deltas = normalize_task_deltas([
        _delta(
            TaskDeltaOperation.UPDATE_GOAL,
            semantic_action="UPDATE_DRAFT",
            title="标题",
            content="正文",
        )
    ])
    assert len(deltas) == 1
    assert deltas[0].desired_changes.get("semantic_action") == "UPDATE_DRAFT"
    assert deltas[0].desired_changes.get("content") == "正文"


def test_title_run_at_timezone_split_preserves_constraints() -> None:
    deltas = normalize_task_deltas([
        _delta(
            TaskDeltaOperation.UPDATE_GOAL,
            semantic_action="UPDATE_DRAFT",
            title="新标题",
            run_at="明天下午4点",
            timezone="Asia/Shanghai",
            goal_id="g1",
            resource_target={"kind": "DRAFT", "resource_id": "d1"},
            description="保留的描述",
        )
    ])
    assert len(deltas) == 2
    assert _actions(deltas) == ["UPDATE_DRAFT", "UPDATE_SCHEDULE"]
    # Cross-cutting goal/reference info is preserved on both.
    for delta in deltas:
        assert delta.desired_changes.get("goal_id") == "g1"
        assert delta.desired_changes.get("resource_target") == {
            "kind": "DRAFT",
            "resource_id": "d1",
        }
    assert deltas[0].desired_changes.get("title") == "新标题"
    assert deltas[0].desired_changes.get("description") == "保留的描述"
    assert deltas[1].desired_changes.get("run_at") == "明天下午4点"
    assert deltas[1].desired_changes.get("timezone") == "Asia/Shanghai"


def test_normalization_is_idempotent() -> None:
    original = _delta(
        TaskDeltaOperation.UPDATE_GOAL,
        semantic_action="UPDATE_DRAFT",
        title="标题",
        run_at="明天下午4点",
    )
    once = normalize_task_deltas([original])
    twice = normalize_task_deltas(once)
    assert _actions(once) == _actions(twice)
    assert len(twice) == 2


def test_create_task_delta_is_untouched() -> None:
    delta = _delta(
        TaskDeltaOperation.CREATE_TASK,
        description="写一篇 Java 文章",
        required_capabilities=["GENERATE_CONTENT"],
    )
    deltas = normalize_task_deltas([delta])
    assert len(deltas) == 1
    assert deltas[0].operation == TaskDeltaOperation.CREATE_TASK
