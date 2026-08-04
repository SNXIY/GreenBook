"""TaskManager facade over ConversationGoal / Artifact / Schedule.

Phase 3: no dedicated Task table. A Task is a typed view of an established
ConversationGoal plus its bound draft/schedule handles. GoalResolver remains
the adapter for ambiguous multi-goal matching; TaskManager owns ACTION
lifecycle decisions (CREATE / UPDATE / CANCEL / QUERY_STATUS) so follow-up
edits prefer the conversation's active task instead of spawning NEW_GOAL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from app.domain import (
    AdaptiveExecutionDecision,
    ConversationGoal,
    GoalMatch,
    GoalResolution,
    TargetContext,
    TurnIntent,
)
from app.goal_resolver import GoalResolver
from app.target_resolver import EntityTargetResolution, TargetResolver

TaskAction = Literal["CREATE", "UPDATE", "CANCEL", "QUERY_STATUS", "CLARIFY", "PASS"]
TaskStatus = Literal[
    "ACTIVE",
    "WAITING",
    "SCHEDULED",
    "COMPLETED",
    "CANCELLED",
    "FAILED",
]


@dataclass(frozen=True)
class TaskView:
    """Facade projection — task_id is the underlying ConversationGoal.id."""

    task_id: str
    conversation_id: str
    user_id: str | None
    type: str
    artifact_id: str | None
    schedule_id: str | None
    status: TaskStatus
    version: int
    summary: str | None = None
    phase: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "type": self.type,
            "artifact_id": self.artifact_id,
            "schedule_id": self.schedule_id,
            "status": self.status,
            "version": self.version,
            "summary": self.summary,
            "phase": self.phase,
        }


@dataclass(frozen=True)
class TaskTurnDecision:
    """ACTION-path decision produced before / around GoalResolver."""

    action: TaskAction
    task: TaskView | None
    goal_resolution: GoalResolution | None
    turn_relation_override: Literal[
        "NEW_GOAL", "CONTINUE", "MODIFY", "CANCEL", "RETRY", "QUERY_STATE"
    ] | None = None
    force_has_target: bool | None = None
    operation_override: str | None = None
    summary: str = ""


_EXPLICIT_CREATE = re.compile(
    r"(?:写一篇|发一篇|创建一篇|新建一篇|再写一篇|再发一篇|新帖子|新发一篇|"
    r"帮我写一篇|帮我发一篇|生成一篇|创作一篇)",
    re.IGNORECASE,
)
_UPDATE_CONTENT = re.compile(
    r"(?:修改|改一下|改成|改为|调整|更新|替换|重写|追加|加入|加上|增加|补充|"
    r"改内容|改标题|加一段|加一点)",
    re.IGNORECASE,
)
_UPDATE_SCHEDULE = re.compile(
    r"(?:发布时间|定时|改时间|调整时间|改成.{0,12}(?:分钟|小时|点)|"
    r"(?:五|5|\d+)\s*分钟\s*(?:之后|后)|延后|延迟|延遲|提前|推迟)",
    re.IGNORECASE,
)
_CANCEL = re.compile(
    r"(?:取消|撤销).{0,12}(?:定时|发布|任务|帖子|草稿)?|"
    r"(?:不要发了|别发了)",
    re.IGNORECASE,
)
_ACTIVE_REF = re.compile(
    r"(?:刚才那个|刚才的|上一[个轮次]?|上一个|那个帖子|这篇|这份草稿|继续改)",
    re.IGNORECASE,
)


class TaskManager:
    """Facade lifecycle manager for ACTION turns."""

    ACTIVE_GOAL_STATUSES = {
        "ACTIVE",
        "WAITING_CLARIFICATION",
        "WAITING_APPROVAL",
        "PAUSED",
    }

    def __init__(
        self,
        goal_resolver: GoalResolver | None = None,
        target_resolver: TargetResolver | None = None,
    ) -> None:
        self.goal_resolver = goal_resolver or GoalResolver()
        self.target_resolver = target_resolver or TargetResolver()

    # ── public API ──────────────────────────────────────────────────────

    def resolve_active_task(
        self,
        goals: Iterable[ConversationGoal],
        focus_goal_refs: list[str] | None = None,
    ) -> TaskView | None:
        """Prefer focus stack, then newest established non-terminal goal."""

        candidates = [
            goal
            for goal in goals
            if goal.status in self.ACTIVE_GOAL_STATUSES and self._is_actionable(goal)
        ]
        if not candidates:
            return None

        focus_ids = self._focus_ids(focus_goal_refs)
        by_id = {goal.goal_id: goal for goal in candidates}
        for goal_id in focus_ids:
            goal = by_id.get(goal_id)
            if goal is not None:
                return self._to_task(goal)

        ordered = sorted(
            candidates,
            key=lambda item: (
                item.updated_at.isoformat() if item.updated_at else "",
                item.version,
            ),
            reverse=True,
        )
        return self._to_task(ordered[0])

    def create_task(
        self,
        *,
        conversation_id: str,
        summary: str | None = None,
        confidence: float = 1.0,
    ) -> TaskTurnDecision:
        del conversation_id, summary
        return TaskTurnDecision(
            action="CREATE",
            task=None,
            goal_resolution=GoalResolution(
                outcome="NEW_GOAL",
                confidence=confidence,
            ),
            turn_relation_override="NEW_GOAL",
            force_has_target=False,
            operation_override="CREATE_POST",
            summary="创建新任务",
        )

    def update_task(
        self,
        task: TaskView,
        *,
        message: str = "",
        confidence: float = 0.95,
    ) -> TaskTurnDecision:
        """Bind a Task AFTER TargetResolver selected it."""

        operation = self._update_operation(message)
        return TaskTurnDecision(
            action="UPDATE",
            task=task,
            goal_resolution=GoalResolution(
                outcome="RESOLVED",
                goal_id=task.task_id,
                candidates=[
                    GoalMatch(
                        goal_id=task.task_id,
                        label=task.summary or task.task_id,
                        score=confidence,
                        resolution_method="RECENT_ACTIVE",
                    )
                ],
                confidence=confidence,
            ),
            turn_relation_override="MODIFY",
            force_has_target=True,
            operation_override=operation,
            summary=f"更新任务 {task.task_id}",
        )

    def cancel_task(
        self,
        task: TaskView,
        *,
        confidence: float = 0.95,
    ) -> TaskTurnDecision:
        """Bind a cancel AFTER TargetResolver selected the task."""

        return TaskTurnDecision(
            action="CANCEL",
            task=task,
            goal_resolution=GoalResolution(
                outcome="RESOLVED",
                goal_id=task.task_id,
                candidates=[
                    GoalMatch(
                        goal_id=task.task_id,
                        label=task.summary or task.task_id,
                        score=confidence,
                        resolution_method="RECENT_ACTIVE",
                    )
                ],
                confidence=confidence,
            ),
            turn_relation_override="CANCEL",
            force_has_target=True,
            operation_override="CANCEL_SCHEDULE",
            summary=f"取消任务 {task.task_id}",
        )

    def get_task_status(self, task: TaskView) -> dict[str, object]:
        return {
            "task_id": task.task_id,
            "status": task.status,
            "phase": task.phase,
            "artifact_id": task.artifact_id,
            "schedule_id": task.schedule_id,
            "version": task.version,
            "summary": task.summary,
        }

    def list_active_tasks(
        self,
        goals: Iterable[ConversationGoal],
        focus_goal_refs: list[str] | None = None,
    ) -> list[TaskView]:
        """Ordered actionable tasks for TargetResolver Layer A."""

        focus_ids = self._focus_ids(focus_goal_refs)
        candidates = [
            goal
            for goal in goals
            if goal.status in self.ACTIVE_GOAL_STATUSES and self._is_actionable(goal)
        ]
        by_id = {goal.goal_id: goal for goal in candidates}
        ordered: list[ConversationGoal] = []
        for goal_id in focus_ids:
            goal = by_id.get(goal_id)
            if goal is not None and goal not in ordered:
                ordered.append(goal)
        rest = sorted(
            [goal for goal in candidates if goal not in ordered],
            key=lambda item: (
                item.updated_at.isoformat() if item.updated_at else "",
                item.version,
            ),
            reverse=True,
        )
        return [self._to_task(goal) for goal in [*ordered, *rest]]

    def resolve_target(
        self,
        *,
        message: str,
        goals: list[ConversationGoal],
        focus_goal_refs: list[str] | None = None,
        conversation_context: dict | None = None,
        candidate_targets=None,
        artifacts=None,
        schedules=None,
    ) -> EntityTargetResolution:
        """Delegate entity resolution — TaskManager does not choose the target."""

        active_tasks = self.list_active_tasks(goals, focus_goal_refs)
        active = self.resolve_active_task(goals, focus_goal_refs)
        return self.target_resolver.resolve_target(
            message=message,
            active_task=active,
            active_tasks=active_tasks,
            goals=goals,
            conversation_context=conversation_context,
            candidate_targets=candidate_targets,
            artifacts=artifacts or (),
            schedules=schedules or (),
        )

    def prepare_action(
        self,
        *,
        message: str,
        decision: AdaptiveExecutionDecision,
        goals: list[ConversationGoal],
        focus_goal_refs: list[str] | None = None,
    ) -> tuple[AdaptiveExecutionDecision, TaskTurnDecision]:
        """Lifecycle only: CREATE / UPDATE / CANCEL. No target binding."""

        active = self.resolve_active_task(goals, focus_goal_refs)
        turn = self._decide_turn(message=message, active=active, goals=goals)
        if turn.turn_relation_override is None:
            return decision, turn
        updated = decision.model_copy(
            update={"turn_relation": turn.turn_relation_override}
        )
        return updated, turn

    def adapt_goal_resolution(
        self,
        *,
        message: str,
        turn_intent: TurnIntent,
        goal_resolution: GoalResolution,
        goals: list[ConversationGoal],
        focus_goal_refs: list[str] | None = None,
        prior: TaskTurnDecision | None = None,
    ) -> tuple[TurnIntent, GoalResolution, TaskTurnDecision]:
        """Adapter only. Never override a TargetResolver-selected goal."""

        decided = prior or self._decide_turn(
            message=message,
            active=self.resolve_active_task(goals, focus_goal_refs),
            goals=goals,
        )

        if decided.action == "CREATE":
            return turn_intent, decided.goal_resolution or goal_resolution, decided

        # UPDATE/CANCEL without a bound task: TargetResolver owns selection.
        if decided.action in {"UPDATE", "CANCEL"} and decided.task is None:
            operation = decided.operation_override or turn_intent.operation
            patched = turn_intent.model_copy(
                update={
                    "operation": operation,  # type: ignore[arg-type]
                    "operation_class": (
                        "SIDE_EFFECT"
                        if operation
                        in {"UPDATE_SCHEDULE", "PUBLISH_NOW", "CANCEL_SCHEDULE"}
                        else "WRITE"
                    ),
                }
            )
            return patched, goal_resolution, decided

        if decided.action in {"UPDATE", "CANCEL"} and decided.task is not None:
            resolution = decided.goal_resolution or self.update_task(
                decided.task, message=message
            ).goal_resolution
            assert resolution is not None
            operation = decided.operation_override or turn_intent.operation
            patched = turn_intent.model_copy(
                update={
                    "operation": operation,  # type: ignore[arg-type]
                    "operation_class": (
                        "SIDE_EFFECT"
                        if operation
                        in {"UPDATE_SCHEDULE", "PUBLISH_NOW", "CANCEL_SCHEDULE"}
                        else (
                            "READ"
                            if str(operation).startswith("QUERY_")
                            else "WRITE"
                        )
                    ),
                }
            )
            return patched, resolution, decided

        return turn_intent, goal_resolution, decided

    def bind_resolved_target(
        self,
        *,
        message: str,
        action: TaskAction,
        task: TaskView,
    ) -> TaskTurnDecision:
        """Apply lifecycle binding after TargetResolver hit."""

        if action == "CANCEL":
            return self.cancel_task(task)
        return self.update_task(task, message=message)

    def focus_refs_for_active(
        self,
        active: TaskView | None,
        focus_goal_refs: list[str] | None,
    ) -> list[str]:
        refs = [str(item) for item in (focus_goal_refs or []) if item]
        if active is None:
            return refs
        head = f"goal:{active.task_id}"
        rest = [ref for ref in refs if ref != head and ref != active.task_id]
        return [head, *rest]

    # ── internals ───────────────────────────────────────────────────────

    def _decide_turn(
        self,
        *,
        message: str,
        active: TaskView | None,
        goals: list[ConversationGoal],
    ) -> TaskTurnDecision:
        del goals, active
        if self._looks_like_explicit_create(message):
            return self.create_task(conversation_id="", summary=message)
        if self._looks_like_cancel(message):
            return self._pending_mutation(
                action="CANCEL",
                message=message,
                operation_override="CANCEL_SCHEDULE",
                turn_relation="CANCEL",
            )
        if self._looks_like_update(message) or self._looks_like_active_ref(message):
            return self._pending_mutation(
                action="UPDATE",
                message=message,
                operation_override=self._update_operation(message),
                turn_relation="MODIFY",
            )
        return TaskTurnDecision(
            action="PASS",
            task=None,
            goal_resolution=None,
            summary="交给 GoalResolver 适配器",
        )

    def _pending_mutation(
        self,
        *,
        action: Literal["UPDATE", "CANCEL"],
        message: str,
        operation_override: str,
        turn_relation: Literal["MODIFY", "CANCEL"],
    ) -> TaskTurnDecision:
        del message
        return TaskTurnDecision(
            action=action,
            task=None,
            goal_resolution=None,
            turn_relation_override=turn_relation,
            force_has_target=True,
            operation_override=operation_override,
            summary=f"{action} pending TargetResolver",
        )

    @staticmethod
    def _looks_like_explicit_create(message: str) -> bool:
        text = message or ""
        if _EXPLICIT_CREATE.search(text):
            # "再写一篇" is create; "修改并写一篇" still create for the new part —
            # Task Bag handles splits elsewhere. Treat explicit create markers as CREATE.
            return True
        return False

    @staticmethod
    def _looks_like_update(message: str) -> bool:
        text = message or ""
        if _EXPLICIT_CREATE.search(text):
            return False
        return bool(
            _UPDATE_CONTENT.search(text)
            or _UPDATE_SCHEDULE.search(text)
            or _ACTIVE_REF.search(text)
        )

    @staticmethod
    def _looks_like_cancel(message: str) -> bool:
        return bool(_CANCEL.search(message or ""))

    @staticmethod
    def _looks_like_active_ref(message: str) -> bool:
        return bool(_ACTIVE_REF.search(message or ""))

    @staticmethod
    def _update_operation(message: str) -> str:
        text = message or ""
        schedule = bool(_UPDATE_SCHEDULE.search(text))
        content_body = bool(
            re.search(
                r"(?:修改内容|改内容|改标题|加入|加上|增加|补充|追加|替换|重写|"
                r"加一段|加一点|实战|内容)",
                text,
                re.IGNORECASE,
            )
        )
        if schedule and not content_body:
            return "UPDATE_SCHEDULE"
        if content_body and schedule:
            # Compound edit: content primary; TurnPlan may still emit schedule Change.
            return "APPEND_CONTENT"
        if content_body:
            return "APPEND_CONTENT"
        if schedule:
            return "UPDATE_SCHEDULE"
        if _UPDATE_CONTENT.search(text):
            return "APPEND_CONTENT"
        return "APPEND_CONTENT"

    @classmethod
    def _is_actionable(cls, goal: ConversationGoal) -> bool:
        if goal.status in {"CANCELLED", "FAILED", "COMPLETED"}:
            return False
        if goal.phase == "PUBLISHED":
            return False
        context = goal.target_context or TargetContext()
        return bool(
            goal.phase not in {"DISCOVERING"}
            or goal.active_target_ref
            or context.content_target
            or context.schedule_target
            or context.publication_target
            or (goal.summary and goal.intent and goal.intent != "UNKNOWN")
        )

    @staticmethod
    def _focus_ids(focus_goal_refs: list[str] | None) -> list[str]:
        ids: list[str] = []
        for ref in focus_goal_refs or []:
            text = str(ref or "").strip()
            if text.lower().startswith("goal:"):
                text = text.split(":", 1)[1]
            if text and text not in ids:
                ids.append(text)
        return ids

    @staticmethod
    def _status_for_goal(goal: ConversationGoal) -> TaskStatus:
        if goal.status in {"CANCELLED"}:
            return "CANCELLED"
        if goal.status in {"FAILED"}:
            return "FAILED"
        if goal.status in {"COMPLETED"} or goal.phase == "PUBLISHED":
            return "COMPLETED"
        if goal.phase == "SCHEDULED":
            return "SCHEDULED"
        if goal.status in {"WAITING_CLARIFICATION", "WAITING_APPROVAL"}:
            return "WAITING"
        return "ACTIVE"

    def _to_task(self, goal: ConversationGoal) -> TaskView:
        context = goal.target_context or TargetContext()
        content = context.content_target
        schedule = context.schedule_target
        artifact_id = None
        if content is not None:
            artifact_id = content.artifact_id or content.target_id
        schedule_id = None
        if schedule is not None:
            schedule_id = schedule.target_id or schedule.schedule_id
        elif content is not None and content.schedule_id:
            schedule_id = content.schedule_id
        return TaskView(
            task_id=goal.goal_id,
            conversation_id=goal.conversation_id,
            user_id=None,
            type=(goal.intent or "CONTENT_PUBLISH").upper(),
            artifact_id=artifact_id,
            schedule_id=schedule_id,
            status=self._status_for_goal(goal),
            version=goal.version,
            summary=goal.summary,
            phase=goal.phase,
        )


task_manager = TaskManager()
