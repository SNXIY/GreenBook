"""Canonical semantic golden cases.

The original baseline stored one expected tuple per category.  That made a
create sentence, a revise sentence, and a target-dependent follow-up share the
same answer.  The cases below keep the 16 x 5 expression set, but every case
owns its product-semantic expectation and any context it needs.

This remains a semantic fixture.  It does not create durable Tasks, Drafts, or
Schedules; the production semantic adapter consumes the bounded context only.
"""

from __future__ import annotations

from typing import Any

from ..models import EvalCase


def _expected(
    action_family: str,
    publication_mode: str,
    temporal_kind: str,
    temporal_resolved: bool,
    target_state: str,
    clarification_required: bool,
    objective_count: int | None,
    task_expectation: str,
) -> dict[str, Any]:
    """Build one explicit canonical expected value for one case."""

    # The migrated contract records target resolution outcome, not whether
    # the model represented a context reference with a concrete id.
    target_state = {
        "CONTEXTUAL": "RESOLVED",
        "EXPLICIT": "RESOLVED",
    }.get(target_state, target_state)
    return {
        "action_family": action_family,
        "publication_mode": publication_mode,
        "temporal_kind": temporal_kind,
        "temporal_resolved": temporal_resolved,
        "target_state": target_state,
        "clarification_required": clarification_required,
        "objective_count": objective_count,
        "task_expectation": task_expectation,
    }


_MESSAGES: dict[str, list[str]] = {
    "QUERY": [
        "我有多少草稿？",
        "How many drafts do I have?",
        "列出我的草稿数量",
        "显示草稿总数",
        "Count my saved drafts",
    ],
    "SEARCH": [
        "找最近的 Java 面试帖子",
        "Search recent Java interview posts",
        "搜索最新的 Agent 文章",
        "帮我查一下热门 Java 内容",
        "Find posts about HashMap",
    ],
    "CREATE_REVISE": [
        "写一篇 Java 学习帖子",
        "Create a short post about agents",
        "帮我起草一篇关于 Redis 的文章",
        "Draft a concise Python tips post",
        "把这篇内容改得更简洁",
    ],
    "DRAFT_ONLY": [
        "写一篇 Java 帖子，只保存草稿",
        "Create a draft only",
        "生成内容但不要发布",
        "保存一篇 Agent 学习草稿",
        "只写，不安排发布时间",
    ],
    "PUBLISH_NOW": [
        "把刚刚那篇现在发布",
        "Publish the latest draft now",
        "立即发布 Java 那篇",
        "现在上线刚写的帖子",
        "Publish this draft immediately",
    ],
    "SCHEDULE": [
        "明天下午 2 点发布 Agent 帖子",
        "Schedule it for tomorrow at 2 PM",
        "五分钟后发布这篇草稿",
        "Set publication for 09:00 tomorrow",
        "安排周五晚上八点发布",
    ],
    "UPDATE_SCHEDULE": [
        "把 Java 那篇改到下午 4 点发布",
        "Move the post to 6 PM tomorrow",
        "将发布时间调整到周一上午九点",
        "Reschedule this draft for 15:30",
        "把发布时间改成明天中午",
    ],
    "CANCEL": [
        "取消 Agent 那篇发布，保留草稿",
        "Cancel the scheduled publication",
        "不要再发布这篇了",
        "撤销明天的发布安排",
        "Stop the pending schedule but keep the draft",
    ],
    "DELETE_HITL": [
        "删除 Java 那篇帖子",
        "Delete the latest post",
        "请移除这份草稿",
        "把 Agent 帖子删掉",
        "Remove that post permanently",
    ],
    "MULTI_OBJECTIVE": [
        "Java 明天 9 点发布，Agent 下午 2 点发布",
        "Schedule Java at 9 and Agent at 2",
        "创建两篇文章并分别安排发布时间",
        "One draft now, one post tomorrow",
        "同时处理三篇不同内容",
    ],
    "CROSS_TURN": [
        "给刚才的草稿加一段 HashMap 示例",
        "Add a HashMap section to the draft",
        "给刚才的 Agent 文章加上示例",
        "Make the introduction of the draft shorter",
        "润色我刚刚创建的那篇草稿",
    ],
    "AMBIGUOUS_TARGET": [
        "把那篇改一下",
        "Update the post",
        "删除那篇文章",
        "Publish one of the posts I mentioned",
        "修改那份草稿",
    ],
    "UNRESOLVED_TEMPORAL": [
        "安排发布，但我还没想好时间",
        "Schedule it sometime later",
        "找个合适时间发布",
        "以后发布这篇",
        "Publish when convenient",
    ],
    "SEARCH_CREATE": [
        "搜索 Java 面试内容后写一篇总结",
        "Search Agent posts and create a summary draft",
        "先找资料，再根据结果写短帖",
        "Research Redis articles then save a draft",
        "Find recent posts and turn them into a draft",
    ],
    "TEMPORAL_SYNONYM": [
        "明早九点发布",
        "tomorrow afternoon at 2",
        "五分钟以后安排发布",
        "下周一 10:30 发出",
        "at 14:00 JST tomorrow",
    ],
    "INVALID_INPUT": [
        "",
        "帮我处理一个",
        "发布",
        "写",
        "schedule",
    ],
}


_CASE_EXPECTATIONS: dict[str, dict[str, Any]] = {
    # QUERY
    "semantic-query-1": _expected("QUERY", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-query-2": _expected("QUERY", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-query-3": _expected("QUERY", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-query-4": _expected("QUERY", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-query-5": _expected("QUERY", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    # SEARCH
    "semantic-search-1": _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search-2": _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search-3": _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search-4": _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search-5": _expected("SEARCH", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    # CREATE and REVISE have independent expectations in one legacy category.
    "semantic-create_revise-1": _expected("CREATE", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-create_revise-2": _expected("CREATE", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-create_revise-3": _expected("CREATE", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-create_revise-4": _expected("CREATE", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-create_revise-5": _expected("REVISE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    # DRAFT_ONLY is a publication constraint, not a new execution operation.
    "semantic-draft_only-1": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-draft_only-2": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-draft_only-3": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-draft_only-4": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-draft_only-5": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    # PUBLISH_NOW is semantic intent; approval is evaluated by Runtime cases.
    "semantic-publish_now-1": _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-publish_now-2": _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-publish_now-3": _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-publish_now-4": _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-publish_now-5": _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "CONTEXTUAL", False, 1, "READY"),
    # Explicit create-and-schedule has no target; pronouns use the setup draft.
    "semantic-schedule-1": _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1, "READY"),
    "semantic-schedule-2": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-schedule-3": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-schedule-4": _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1, "READY"),
    "semantic-schedule-5": _expected("SCHEDULE", "SCHEDULED", "FUTURE", True, "NONE", False, 1, "READY"),
    "semantic-update_schedule-1": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-update_schedule-2": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-update_schedule-3": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-update_schedule-4": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-update_schedule-5": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cancel-1": _expected("CANCEL", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cancel-2": _expected("CANCEL", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cancel-3": _expected("CANCEL", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cancel-4": _expected("CANCEL", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cancel-5": _expected("CANCEL", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-delete_hitl-1": _expected("DELETE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-delete_hitl-2": _expected("DELETE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-delete_hitl-3": _expected("DELETE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-delete_hitl-4": _expected("DELETE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-delete_hitl-5": _expected("DELETE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    # Objective count is per message: 2, 2, 2, 2, and 3 respectively.
    "semantic-multi_objective-1": _expected("MULTI_OBJECTIVE", "SCHEDULED", "FUTURE", True, "NONE", False, 2, "READY"),
    "semantic-multi_objective-2": _expected("MULTI_OBJECTIVE", "SCHEDULED", "FUTURE", True, "NONE", False, 2, "READY"),
    "semantic-multi_objective-3": _expected("MULTI_OBJECTIVE", "UNRESOLVED", "UNRESOLVED", False, "NONE", True, 2, "CLARIFY"),
    "semantic-multi_objective-4": _expected("MULTI_OBJECTIVE", "MIXED", "MIXED", True, "NONE", True, 2, "CLARIFY"),
    "semantic-multi_objective-5": _expected("MULTI_OBJECTIVE", "NONE", "NONE", False, "NONE", True, 3, "CLARIFY"),
    "semantic-cross_turn-1": _expected("REVISE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cross_turn-2": _expected("REVISE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cross_turn-3": _expected("REVISE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cross_turn-4": _expected("REVISE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-cross_turn-5": _expected("REVISE", "NONE", "NONE", False, "CONTEXTUAL", False, 1, "READY"),
    "semantic-ambiguous_target-1": _expected("REVISE", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
    "semantic-ambiguous_target-2": _expected("REVISE", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
    "semantic-ambiguous_target-3": _expected("DELETE", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
    "semantic-ambiguous_target-4": _expected("PUBLISH_NOW", "IMMEDIATE", "NOW", True, "AMBIGUOUS", True, 1, "CLARIFY"),
    "semantic-ambiguous_target-5": _expected("REVISE", "NONE", "NONE", False, "AMBIGUOUS", True, 1, "CLARIFY"),
    "semantic-unresolved_temporal-1": _expected("UPDATE_SCHEDULE", "UNRESOLVED", "UNRESOLVED", False, "CONTEXTUAL", True, 1, "CLARIFY"),
    "semantic-unresolved_temporal-2": _expected("UPDATE_SCHEDULE", "UNRESOLVED", "UNRESOLVED", False, "CONTEXTUAL", True, 1, "CLARIFY"),
    "semantic-unresolved_temporal-3": _expected("UPDATE_SCHEDULE", "UNRESOLVED", "UNRESOLVED", False, "CONTEXTUAL", True, 1, "CLARIFY"),
    "semantic-unresolved_temporal-4": _expected("UPDATE_SCHEDULE", "UNRESOLVED", "UNRESOLVED", False, "CONTEXTUAL", True, 1, "CLARIFY"),
    "semantic-unresolved_temporal-5": _expected("UPDATE_SCHEDULE", "UNRESOLVED", "UNRESOLVED", False, "CONTEXTUAL", True, 1, "CLARIFY"),
    # Search + synthesis is one deliverable under the current Objective contract.
    "semantic-search_create-1": _expected("CREATE", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search_create-2": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search_create-3": _expected("CREATE", "NONE", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search_create-4": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-search_create-5": _expected("CREATE", "DRAFT_ONLY", "NONE", False, "NONE", False, 1, "READY"),
    "semantic-temporal_synonym-1": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-temporal_synonym-2": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-temporal_synonym-3": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-temporal_synonym-4": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-temporal_synonym-5": _expected("UPDATE_SCHEDULE", "SCHEDULED", "FUTURE", True, "CONTEXTUAL", False, 1, "READY"),
    "semantic-invalid_input-1": _expected("INVALID", "NONE", "NONE", False, "NONE", True, None, "CLARIFY"),
    "semantic-invalid_input-2": _expected("CLARIFY", "NONE", "NONE", False, "NONE", True, None, "CLARIFY"),
    "semantic-invalid_input-3": _expected("CLARIFY", "NONE", "NONE", False, "NONE", True, None, "CLARIFY"),
    "semantic-invalid_input-4": _expected("CREATE", "NONE", "NONE", False, "NONE", True, 1, "CLARIFY"),
    "semantic-invalid_input-5": _expected("CLARIFY", "NONE", "NONE", False, "NONE", True, None, "CLARIFY"),
}


def _target_context(*, ambiguous: bool = False) -> dict[str, Any]:
    """Return explicit, in-memory semantic target candidates."""

    count = 2 if ambiguous else 1
    targets: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for index in range(1, count + 1):
        task_id = f"evaluation-task-{index}"
        objective_id = f"evaluation-objective-{index}"
        if ambiguous:
            label = "Java post" if index == 1 else "Python post"
        else:
            label = "Java Agent draft" if index == 1 else "Python Agent draft"
        resource_rows = [
            ("TASK", task_id, task_id),
            ("DRAFT", f"evaluation-draft-{index}", f"evaluation-draft-{index}"),
            ("POST", f"evaluation-post-{index}", f"evaluation-post-{index}"),
            ("SCHEDULE", f"evaluation-schedule-{index}", f"evaluation-schedule-{index}"),
        ]
        for kind, identity, resource_id in resource_rows:
            targets.append({
                "kind": kind,
                "id": identity,
                "resource_id": resource_id,
                "task_id": task_id,
                "label": label,
                "status": "SCHEDULED" if kind == "SCHEDULE" else "ACTIVE",
                "created_at": f"2026-08-19T0{index}:00:00+08:00",
                "metadata": {"objective_id": objective_id},
            })
        tasks.append({
            "task_id": task_id,
            "goal": label,
            "objective_id": objective_id,
            "resource_index": [
                {"resource_id": resource_id, "resource_kind": kind, "objective_id": objective_id}
                for kind, _, resource_id in resource_rows
            ],
        })
    return {"targets": targets, "active_tasks": tasks}


def _case_context(category: str, index: int) -> dict[str, Any]:
    if category == "AMBIGUOUS_TARGET":
        return _target_context(ambiguous=True)
    if category in {
        "PUBLISH_NOW",
        "UPDATE_SCHEDULE",
        "CANCEL",
        "DELETE_HITL",
        "UNRESOLVED_TEMPORAL",
        "TEMPORAL_SYNONYM",
        "CROSS_TURN",
    }:
        return _target_context()
    if category == "CREATE_REVISE" and index == 5:
        return _target_context()
    if category == "SCHEDULE" and index in {2, 3}:
        return _target_context()
    return {}


def _case_history(category: str, index: int) -> list[dict[str, str]]:
    if category in {
        "PUBLISH_NOW",
        "TEMPORAL_SYNONYM",
        "UNRESOLVED_TEMPORAL",
        "CROSS_TURN",
    } or (category == "CREATE_REVISE" and index == 5):
        return [{"role": "user", "content": "Create a short post about agents"}]
    if category == "SCHEDULE" and index in {2, 3}:
        return [{"role": "user", "content": "Create a short post about agents"}]
    if category in {"UPDATE_SCHEDULE", "CANCEL", "DELETE_HITL"}:
        return [{"role": "user", "content": "Create a short post about agents"}]
    if category == "AMBIGUOUS_TARGET":
        return [{"role": "user", "content": "Create two different drafts"}]
    return []


def semantic_baseline_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for category, messages in _MESSAGES.items():
        for index, message in enumerate(messages, start=1):
            case_id = f"semantic-{category.lower()}-{index}"
            cases.append(EvalCase(
                case_id=case_id,
                category=category,
                description="Canonical product-semantic golden case",
                user_message=message,
                conversation_turns=_case_history(category, index),
                setup_context=_case_context(category, index),
                expected_semantic_state=dict(_CASE_EXPECTATIONS[case_id]),
                # The canonical state is the only semantic oracle.  These
                # legacy scalar fields stay unset so an internal enum cannot
                # create a second, conflicting contract.
                expected_objective_count=None,
                expected_temporal_resolution=None,
                expected_clarification=None,
                expected_task_state=None,
            ))
    return cases


SEMANTIC_BASELINE_CASES = semantic_baseline_cases()

__all__ = ["SEMANTIC_BASELINE_CASES", "semantic_baseline_cases"]
