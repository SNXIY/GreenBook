"""Conversation-level multi-task state and structured target resolution.

This module is intentionally a read-model/coordinator boundary.  It does not
own ExecutionState, queue leases, worker consumption, or external resources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Iterable, Sequence

from .intent_models import ActionType, IntentSpec
from .models import (
    ArtifactRef,
    Task,
    TaskExecutionRef,
    TaskGoal,
    TaskResourceRef,
    TaskStatus,
)


_ORDINALS = {
    "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_ORDINAL_RE = re.compile(r"(?:第\s*([一二两三四五六七八九十\d]+)\s*[个项篇]?|([一二两三四五六七八九十\d]+)\s*[个项篇])")
_TITLE_RE = re.compile(r"[《「\"']([^》」\"']+)[》」\"']")


@dataclass(frozen=True, slots=True)
class TaskSegment:
    """One independently understood request extracted from a turn."""

    index: int
    text: str
    is_query: bool = False


@dataclass(frozen=True, slots=True)
class TargetResolution:
    task: Task | None
    candidates: tuple[Task, ...] = ()
    reason: str = ""

    @property
    def is_ambiguous(self) -> bool:
        return len(self.candidates) > 1


@dataclass(frozen=True, slots=True)
class IntentDelta:
    """Cross-turn changes applied to an existing Task/resource projection."""

    target_task_ids: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    title: str | None = None
    schedule_at: str | None = None
    content_append: str | None = None

    @classmethod
    def from_message(
        cls, message: str, *, target_task_ids: Iterable[str] = (),
    ) -> "IntentDelta":
        operations: list[str] = []
        lowered = message.lower()
        if "标题" in message or "title" in lowered:
            operations.append("UPDATE_TITLE")
        if any(token in message for token in ("发布时间", "定时", "提前", "延后", "下午", "上午", "schedule")):
            operations.append("UPDATE_SCHEDULE")
        if ("取消发布" in message or "取消定时" in message or ("取消" in message and "发布" in message)) or "cancel" in lowered:
            operations.append("CANCEL_SCHEDULE")
        if any(token in message for token in ("增加", "补充", "添加", "append")):
            operations.append("UPDATE_CONTENT")
        title_match = _TITLE_RE.search(message)
        content = None
        if operations and "UPDATE_CONTENT" in operations:
            content = message
        return cls(
            target_task_ids=tuple(target_task_ids),
            operations=tuple(dict.fromkeys(operations)),
            title=title_match.group(1).strip() if title_match else None,
            schedule_at=message if "UPDATE_SCHEDULE" in operations else None,
            content_append=content,
        )


def split_task_segments(message: str) -> list[TaskSegment]:
    """Split explicit independent requests while preserving each request.

    This is deliberately conservative: only numbered/ordinal top-level
    clauses and a clear trailing ``然后`` query are split.  A normal sentence
    remains one Task and is still understood by the existing provider.
    """

    text = " ".join(message.strip().split())
    if not text:
        return [TaskSegment(index=0, text="")]

    # ``第一，... 第二，...`` and ``1. ... 2. ...``.
    matches = list(re.finditer(r"第\s*[一二两三四五六七八九十\d]+\s*[，,:：]", text))
    if len(matches) < 2:
        matches = list(re.finditer(r"\d+\s*[.、)]\s*", text))
    if len(matches) >= 2:
        chunks: list[TaskSegment] = []
        for pos, match in enumerate(matches):
            start = match.end()
            end = matches[pos + 1].start() if pos + 1 < len(matches) else len(text)
            chunk = text[start:end].strip(" ，,。；;\n")
            if chunk:
                chunks.append(TaskSegment(index=len(chunks), text=chunk))
        if len(chunks) >= 2:
            return _mark_queries(chunks)

    # A common conversational form uses two complete clauses followed by a
    # read-only question: ``...；...。然后分析...``.
    query_match = re.search(r"(?:[。；;]\s*)然后\s*(?=(?:分析|查询|告诉我|what|which))", text, re.I)
    if query_match:
        left = text[:query_match.start()].strip(" ，,。；;")
        right = text[query_match.end():].strip(" ，,。；;")
        if left and right:
            # Preserve independent action clauses on the left when possible.
            left_segments = [
                TaskSegment(index=i, text=part.strip(" ，,。；;"))
                for i, part in enumerate(re.split(r"[；;]", left))
                if part.strip(" ，,。；;")
            ]
            if len(left_segments) <= 1:
                left_segments = split_task_segments(left)
            if len(left_segments) > 1:
                return [*left_segments, TaskSegment(index=len(left_segments), text=right, is_query=True)]
            return [TaskSegment(index=0, text=left), TaskSegment(index=1, text=right, is_query=True)]

    return [TaskSegment(index=0, text=text, is_query=_looks_like_query(text))]


def _mark_queries(segments: list[TaskSegment]) -> list[TaskSegment]:
    return [TaskSegment(s.index, s.text, _looks_like_query(s.text)) for s in segments]


def _looks_like_query(text: str) -> bool:
    return bool(re.search(r"(?:只告诉我|告诉我|查询|分析一下|总结一下|what|which|仅查询)", text, re.I)) and not bool(
        re.search(r"(?:写一篇|生成|创建|发布|取消|改成|修改|提前|增加)", text, re.I)
    )


def parse_ordinal(text: str) -> int | None:
    match = _ORDINAL_RE.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(2) or ""
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if len(raw) == 2 and raw[0] == "十":
        return 10 + _ORDINALS.get(raw[1], 0)
    if len(raw) == 2 and raw[1] == "十":
        return _ORDINALS.get(raw[0], 0) * 10
    return _ORDINALS.get(raw)


class ConversationTaskIndex:
    """Conversation-scoped structured Task index.

    The Task record remains canonical for ownership/lifecycle.  This index
    only adds goal, execution, resource, and last-action projections needed by
    target resolution and the conversation UI.
    """

    def __init__(self, tasks: Sequence[Task] = ()) -> None:
        self._tasks: dict[str, Task] = {task.task_id: task for task in tasks}

    def register(self, task: Task) -> Task:
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda task: (task.created_at, task.task_id))

    def record_goal(self, task_id: str, goal: TaskGoal) -> None:
        task = self._tasks[task_id]
        task.goals = [item for item in task.goals if item.goal_id != goal.goal_id]
        task.goals.append(goal)
        task.updated_at = _now()

    def record_execution(self, task_id: str, execution: TaskExecutionRef) -> None:
        task = self._tasks[task_id]
        task.execution_refs = [
            item for item in task.execution_refs
            if item.execution_id != execution.execution_id
        ]
        task.execution_refs.append(execution)
        task.updated_at = _now()

    def record_resource(self, task_id: str, resource: TaskResourceRef) -> None:
        task = self._tasks[task_id]
        task.resource_index = [
            item for item in task.resource_index
            if item.resource_id != resource.resource_id
        ]
        task.resource_index.append(resource)
        task.updated_at = _now()

    def mark_action(self, task_id: str, action: str) -> None:
        task = self._tasks[task_id]
        task.last_action = action
        if action not in task.action_history:
            task.action_history.append(action)
        task.updated_at = _now()

    def resolve(self, text: str, *, include_cancelled: bool = True) -> TargetResolution:
        return ConversationTargetResolver().resolve(text, self.list(), include_cancelled=include_cancelled)

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "task_id": task.task_id,
                "status": task.status.value,
                "goal": task.goal,
                "goals": [goal.model_dump(mode="json") for goal in task.goals],
                "execution_ids": [ref.execution_id for ref in task.execution_refs],
                "resources": [resource.model_dump(mode="json") for resource in task.resource_index],
                "updated_at": task.updated_at,
            }
            for task in self.list()
        ]


class ConversationTargetResolver:
    """Resolve explicit, ordinal, weak, and resource references structurally."""

    def resolve(
        self,
        text: str,
        tasks: Sequence[Task],
        *,
        include_cancelled: bool = True,
    ) -> TargetResolution:
        candidates = [task for task in tasks if include_cancelled or task.status != TaskStatus.CANCELLED]
        if not candidates:
            return TargetResolution(None, reason="no_tasks")

        ordinal = parse_ordinal(text)
        if ordinal is not None:
            if 1 <= ordinal <= len(candidates):
                return TargetResolution(candidates[ordinal - 1], reason="ordinal")
            return TargetResolution(None, reason="ordinal_out_of_range")

        label_matches = self._label_matches(text, candidates)
        if len(label_matches) == 1:
            return TargetResolution(label_matches[0], reason="structured_label")
        if len(label_matches) > 1:
            return TargetResolution(None, tuple(label_matches), reason="ambiguous_label")

        filtered = candidates
        if "取消发布" in text or "cancel" in text.lower():
            filtered = [
                task for task in candidates
                if task.last_action == "CANCEL_SCHEDULE"
                or "CANCEL_SCHEDULE" in task.action_history
                or any((resource.status or "").upper() == "CANCELLED" for resource in task.resource_index)
            ]
        elif "改过标题" in text or "标题" in text and ("刚才" in text or "那个" in text):
            filtered = [
                task for task in candidates
                if task.last_action == "UPDATE_TITLE"
                or "UPDATE_TITLE" in task.action_history
            ]

        if len(filtered) == 1:
            return TargetResolution(filtered[0], reason="weak_structured")
        if len(filtered) > 1:
            return TargetResolution(None, tuple(filtered), reason="ambiguous_weak_reference")

        if any(token in text for token in ("刚才", "前一篇", "那篇", "那个")):
            # Recency is only safe when it identifies one task.  We use the
            # stable conversation order for ``前一篇`` and last-action index
            # for ``刚才``; never fall back to an arbitrary active task.
            ordered = sorted(candidates, key=lambda task: (task.updated_at, task.task_id), reverse=True)
            if "前一篇" in text and len(ordered) >= 2:
                return TargetResolution(ordered[-2], reason="previous")
            if len(ordered) == 1:
                return TargetResolution(ordered[0], reason="recent_unique")
            return TargetResolution(None, tuple(ordered), reason="ambiguous_recent")
        if len(candidates) > 1 and any(
            token in text for token in ("优化", "修改", "改一下", "调整", "improve", "update")
        ):
            return TargetResolution(None, tuple(candidates), reason="ambiguous_unscoped_action")
        return TargetResolution(None, reason="unmatched")

    @staticmethod
    def _label_matches(text: str, tasks: Sequence[Task]) -> list[Task]:
        matches: list[Task] = []
        for task in tasks:
            labels = [task.goal, task.goal_summary or ""]
            labels.extend(resource.title or "" for resource in task.resource_index)
            labels.extend(resource.resource_kind for resource in task.resource_index)
            if any(label and label in text for label in labels):
                matches.append(task)
                continue
            # Domain labels are useful when the title is represented only by a
            # resource record or a goal sentence.
            terms = set(re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{1,30}", text.lower()))
            haystack = " ".join(labels).lower()
            if terms and any(term in haystack for term in terms):
                matches.append(task)
        return matches


def apply_intent_delta(task: Task, delta: IntentDelta) -> Task:
    """Update only the conversation projection; external tools remain separate."""

    now = _now()
    if delta.title:
        for resource in task.resource_index:
            if resource.resource_kind.upper() in {"DRAFT", "POST", "CONTENT"}:
                resource.title = delta.title
                resource.updated_at = now
        task.last_action = "UPDATE_TITLE"
        if "UPDATE_TITLE" not in task.action_history:
            task.action_history.append("UPDATE_TITLE")
    if delta.schedule_at:
        for resource in task.resource_index:
            if resource.resource_kind.upper() == "SCHEDULE":
                resource.scheduled_at = delta.schedule_at
                resource.status = "ACTIVE"
                resource.updated_at = now
        task.last_action = "UPDATE_SCHEDULE"
        if "UPDATE_SCHEDULE" not in task.action_history:
            task.action_history.append("UPDATE_SCHEDULE")
    if "CANCEL_SCHEDULE" in delta.operations:
        for resource in task.resource_index:
            if resource.resource_kind.upper() == "SCHEDULE":
                resource.status = "CANCELLED"
                resource.updated_at = now
        task.last_action = "CANCEL_SCHEDULE"
        if "CANCEL_SCHEDULE" not in task.action_history:
            task.action_history.append("CANCEL_SCHEDULE")
    task.updated_at = now
    return task


def intent_delta_from_spec(spec: IntentSpec, message: str, task_ids: Iterable[str] = ()) -> IntentDelta:
    operations: list[str] = []
    for action in spec.actions:
        if action.action == ActionType.UPDATE:
            operations.append("UPDATE_TITLE" if "标题" in message or "title" in message.lower() else "UPDATE_CONTENT")
        elif action.action == ActionType.DELETE:
            operations.append("CANCEL_SCHEDULE")
        elif action.action == ActionType.PUBLISH:
            operations.append("UPDATE_SCHEDULE")
    parsed = IntentDelta.from_message(message, target_task_ids=task_ids)
    return IntentDelta(
        target_task_ids=parsed.target_task_ids,
        operations=tuple(dict.fromkeys([*operations, *parsed.operations])),
        title=parsed.title,
        schedule_at=parsed.schedule_at,
        content_append=parsed.content_append,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "ConversationTaskIndex",
    "ConversationTargetResolver",
    "IntentDelta",
    "TargetResolution",
    "TaskSegment",
    "apply_intent_delta",
    "intent_delta_from_spec",
    "parse_ordinal",
    "split_task_segments",
]
