"""Deterministic cross-turn reference extraction + target resolution safety."""

from __future__ import annotations

from greenbook_agent_core.command.models import Command, CommandContext, CommandType
from greenbook_agent_core.command.reference_extractor import ReferenceExtractor
from greenbook_agent_core.command.target import TargetResolver, TargetResolutionStatus


def _task(tid: str, label: str, created: str, run_at: str | None = None) -> dict:
    item = {
        "id": tid, "task_id": tid, "kind": "TASK", "label": label, "goal": label,
        "created_at": created, "updated_at": created,
    }
    if run_at:
        item["run_at"] = run_at
    return item


def _resolve(
    text: str,
    targets: list[dict],
    timezone: str = "Asia/Shanghai",
    *,
    semantic_operation: str = "",
):
    ctx = CommandContext(timezone=timezone, targets=targets)
    feature = ReferenceExtractor().extract(text)
    target = feature.to_command_target() if feature else None
    command = Command(
        type=CommandType.MODIFY,
        goal=text,
        target=target,
        raw_input=text,
        semantic_operation=semantic_operation,
    )
    return TargetResolver().resolve(command, ctx)


def test_extracts_topic_token() -> None:
    feature = ReferenceExtractor().extract("Java 那篇改到下午四点")
    assert feature is not None
    assert feature.topic == "Java"
    assert feature.temporal_word is None  # the 下午 is the new run_at, not a target


def test_extracts_proximal_marker() -> None:
    feature = ReferenceExtractor().extract("刚刚那篇正文再精简一点")
    assert feature is not None
    assert feature.reference_type.value == "ACTIVE"


def test_extracts_ordinal() -> None:
    feature = ReferenceExtractor().extract("第一篇再补一段 HashMap")
    assert feature is not None
    assert feature.ordinal == 1


def test_extracts_temporal_before_reference() -> None:
    feature = ReferenceExtractor().extract("把下午那篇改到晚上八点")
    assert feature is not None
    assert feature.temporal_word == "下午"


def test_extracts_resource_hint() -> None:
    feature = ReferenceExtractor().extract("那个草稿删掉")
    assert feature is not None
    assert feature.kind.value == "DRAFT"
    assert feature.reference_type.value == "ACTIVE"


def test_generic_reference_preserves_ambiguity() -> None:
    result = _resolve(
        "把那篇改一下",
        [
            _task("t-java", "Java post", "2026-08-16T01:00:00+00:00"),
            _task("t-python", "Python post", "2026-08-16T02:00:00+00:00"),
        ],
    )
    assert result.status == TargetResolutionStatus.AMBIGUOUS
    assert {candidate.id for candidate in result.candidates} == {"t-java", "t-python"}


def test_generic_resource_reference_keeps_typed_kind() -> None:
    feature = ReferenceExtractor().extract("修改那份草稿")
    assert feature is not None
    assert feature.kind.value == "DRAFT"
    assert feature.reference_type.value == "ACTIVE"

    result = _resolve(
        "修改那份草稿",
        [
            {"kind": "DRAFT", "id": "draft-1", "label": "Java"},
            {"kind": "DRAFT", "id": "draft-2", "label": "Python"},
            {"kind": "POST", "id": "post-1", "label": "Java"},
        ],
    )
    assert result.status == TargetResolutionStatus.AMBIGUOUS
    assert {candidate.kind.value for candidate in result.candidates} == {"DRAFT"}


def test_active_reference_with_no_candidates_is_not_found() -> None:
    result = _resolve("Update the post", [])
    assert result.status == TargetResolutionStatus.NOT_FOUND


def test_explicit_identity_disambiguates_generic_candidates() -> None:
    command = Command(
        type=CommandType.MODIFY,
        goal="update post",
        target={"kind": "POST", "id": "post-2", "reference_type": "IDENTIFIER"},
    )
    result = TargetResolver().resolve(command, {
        "targets": [
            {"kind": "POST", "id": "post-1", "label": "Java"},
            {"kind": "POST", "id": "post-2", "label": "Python"},
        ]
    })
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None and result.target.id == "post-2"


def test_extracts_explicit_id() -> None:
    feature = ReferenceExtractor().extract("修改 347200731104808960 的标题")
    assert feature is not None
    assert feature.id == "347200731104808960"


def test_topic_resolves_unique_target() -> None:
    result = _resolve(
        "Java 那篇改到下午四点",
        [_task("t-java", "Java 集合详解教程", "2026-08-16T01:00:00+00:00")],
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.id == "t-java"


def test_topic_resolves_does_not_match_irrelevant_label() -> None:
    result = _resolve(
        "Java 那篇改到下午四点",
        [_task("t-spring", "Spring Boot 实战", "2026-08-16T01:00:00+00:00")],
    )
    assert result.status != TargetResolutionStatus.RESOLVED


def test_temporal_multiple_candidates_must_clarify() -> None:
    # JVM 14:00 and Spring Boot 17:00 both fall in the 下午 (12-18) window.
    result = _resolve(
        "把下午那篇改到晚上八点",
        [
            _task("t-jvm", "JVM 内存模型", "2026-08-16T01:00:00+00:00",
                  "2026-08-16T06:00:00+00:00"),
            _task("t-spring", "Spring Boot 实战", "2026-08-16T02:00:00+00:00",
                  "2026-08-16T09:00:00+00:00"),
        ],
    )
    assert result.status == TargetResolutionStatus.AMBIGUOUS
    assert result.target is None


def test_temporal_single_candidate_resolves() -> None:
    result = _resolve(
        "把下午那篇改到晚上八点",
        [_task("t-jvm", "JVM 内存模型", "2026-08-16T01:00:00+00:00",
               "2026-08-16T06:00:00+00:00")],
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.id == "t-jvm"


def test_ordinal_uses_creation_order() -> None:
    result = _resolve(
        "第一篇再补一段 HashMap",
        [
            _task("t1", "Java 集合详解", "2026-08-16T01:00:00+00:00"),
            _task("t2", "JVM GC 优化", "2026-08-16T02:00:00+00:00"),
        ],
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.id == "t1"


def test_proximal_single_candidate_resolves() -> None:
    result = _resolve(
        "刚刚那篇正文再精简一点",
        [_task("t1", "Redis 学习指南", "2026-08-16T01:00:00+00:00")],
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.id == "t1"


def test_title_reference_resolves_unique_candidate() -> None:
    result = TargetResolver().resolve(
        Command(
            type=CommandType.MODIFY,
            goal="update Java",
            target={
                "kind": "DRAFT",
                "reference_type": "PROPERTY",
                "property": "label",
                "value": "Java",
            },
        ),
        {
            "targets": [
                {"kind": "DRAFT", "id": "draft-java", "label": "Java guide"},
                {"kind": "DRAFT", "id": "draft-python", "label": "Python guide"},
            ]
        },
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None and result.target.id == "draft-java"


def test_operation_scope_resolves_only_legal_typed_kind() -> None:
    result = _resolve(
        "update this draft",
        [
            {"kind": "DRAFT", "id": "draft-1", "label": "Java"},
            {"kind": "POST", "id": "post-1", "label": "Java"},
        ],
        semantic_operation="UPDATE_DRAFT",
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None
    assert result.target.kind.value == "DRAFT"
    assert result.target.id == "draft-1"


def test_operation_scope_keeps_same_kind_candidates_ambiguous() -> None:
    result = _resolve(
        "update this draft",
        [
            {"kind": "DRAFT", "id": "draft-1", "label": "Java"},
            {"kind": "DRAFT", "id": "draft-2", "label": "Python"},
            {"kind": "POST", "id": "post-1", "label": "Java"},
        ],
        semantic_operation="UPDATE_DRAFT",
    )
    assert result.status == TargetResolutionStatus.AMBIGUOUS
    assert {candidate.kind.value for candidate in result.candidates} == {"DRAFT"}
    assert {candidate.id for candidate in result.candidates} == {"draft-1", "draft-2"}


def test_cross_turn_proximal_reference_resolves_unique_candidate() -> None:
    result = _resolve(
        "edit the post",
        [
            {
                "kind": "POST",
                "id": "post-recent",
                "label": "Agent update",
                "created_at": "2026-08-19T02:00:00Z",
            },
        ],
    )
    assert result.status == TargetResolutionStatus.RESOLVED
    assert result.target is not None and result.target.id == "post-recent"


def test_cross_turn_proximal_reference_with_equal_candidates_is_ambiguous() -> None:
    result = _resolve(
        "edit the post",
        [
            {"kind": "POST", "id": "post-a", "label": "Agent A"},
            {"kind": "POST", "id": "post-b", "label": "Agent B"},
        ],
    )
    assert result.status == TargetResolutionStatus.AMBIGUOUS
    assert {candidate.id for candidate in result.candidates} == {"post-a", "post-b"}


def test_weak_task_reference_discovers_tasks_from_separate_context_field() -> None:
    command = Command(
        type=CommandType.MODIFY,
        target={"kind": "TASK", "reference_type": "ACTIVE"},
    )
    result = TargetResolver().resolve(
        command,
        {
            "active_tasks": [
                {"task_id": "task-a", "goal": "Java"},
                {"task_id": "task-b", "goal": "Agent"},
            ],
            "targets": [],
        },
    )
    assert result.status == TargetResolutionStatus.AMBIGUOUS
    assert {candidate.id for candidate in result.candidates} == {"task-a", "task-b"}
