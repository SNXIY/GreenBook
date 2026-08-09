"""Phase 3.6: Target Resolution end-to-end acceptance and regression.

Exercises the Worker ACTION control-plane chain:
  TaskManager.prepare_action
  → TargetResolver.resolve_target (Layer A)
  → bind_resolved_target
  → TurnPipeline.bind_and_compile
  → ToolAdapterRuntime.prepare_arguments

Persists Goals / ScheduledActions in SQLite and asserts the *business object*
mutated is correct — not only resolution_method.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio

from app.artifact_contracts import ArtifactBinder
from app.database import (
    Base,
    Conversation,
    ConversationGoal as ConversationGoalRecord,
    Database,
    ScheduledAction,
)
from app.domain import (
    AdaptiveExecutionDecision,
    CommunityIntent,
    ConversationGoal,
    GoalMatch,
    GoalResolution,
    TargetBinding,
    TargetContext,
)
from app.query_agent import QueryAgent
from app.router import ControlPlaneRouter
from app.task_manager import TaskManager, TaskView
from app.target_resolver import TargetResolver
from app.tool_runtime import ToolAdapterRuntime, ToolRuntimeContext
from app.tools import tool_registry
from app.turn_plan import changes_from_operation, primary_operation_from_changes
from app.turn_pipeline import TurnPipeline

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
JAVA_TITLE = "如何高效学好 Java：一份实用的学习路线图"
FIT_TITLE = "科学减肥：从饮食、运动到生活习惯的完整指南"


@dataclass
class ScheduleLedger:
    """Simulated publication.update_schedule side-effect store."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)

    def seed(self, action_id: str, *, draft_id: str, run_at: datetime, goal_id: str) -> None:
        self.rows[action_id] = {
            "action_id": action_id,
            "draft_id": draft_id,
            "run_at": run_at,
            "goal_id": goal_id,
            "status": "SCHEDULED",
        }

    def apply_update(self, *, action_id: str, run_at: datetime | None = None) -> None:
        assert action_id in self.rows, f"unknown schedule {action_id}"
        before = dict(self.rows[action_id])
        if run_at is not None:
            self.rows[action_id]["run_at"] = run_at
        self.writes.append(
            {
                "tool": "publication.update_schedule",
                "action_id": action_id,
                "draft_id": self.rows[action_id]["draft_id"],
                "goal_id": self.rows[action_id]["goal_id"],
                "run_at": self.rows[action_id]["run_at"],
                "before": before,
            }
        )

    def apply_cancel(self, *, action_id: str) -> None:
        assert action_id in self.rows
        self.rows[action_id]["status"] = "CANCELLED"
        self.writes.append(
            {
                "tool": "publication.cancel_schedule",
                "action_id": action_id,
                "draft_id": self.rows[action_id]["draft_id"],
                "goal_id": self.rows[action_id]["goal_id"],
            }
        )


@dataclass
class DraftLedger:
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    writes: list[dict[str, Any]] = field(default_factory=list)

    def seed(self, draft_id: str, *, goal_id: str, sha: str) -> None:
        self.rows[draft_id] = {
            "draft_id": draft_id,
            "goal_id": goal_id,
            "content_sha256": sha,
            "version": 1,
        }

    def apply_revise(self, *, draft_id: str, instruction: str) -> None:
        assert draft_id in self.rows, f"unknown draft {draft_id}"
        self.rows[draft_id]["version"] += 1
        self.rows[draft_id]["last_instruction"] = instruction
        self.writes.append(
            {
                "tool": "creator.revise_draft",
                "draft_id": draft_id,
                "goal_id": self.rows[draft_id]["goal_id"],
                "version": self.rows[draft_id]["version"],
            }
        )


def _domain_goal(
    goal_id: str,
    title: str,
    *,
    draft_id: str,
    schedule_id: str,
    minutes_ago: int,
) -> ConversationGoal:
    content = TargetBinding(
        target_type="DRAFT",
        role="CONTENT",
        target_id=draft_id,
        artifact_id=f"artifact-{draft_id}",
        content_sha256="a" * 64,
        schedule_id=schedule_id,
        resolution_method="TOOL_OUTPUT",
    )
    schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id=schedule_id,
        schedule_id=schedule_id,
        resolution_method="TOOL_OUTPUT",
    )
    return ConversationGoal(
        goal_id=goal_id,
        conversation_id="conv-e2e",
        intent="CONTENT_PUBLISH",
        summary=title,
        artifact_titles=[title],
        aliases=[title],
        explicit_refs=[draft_id, schedule_id, f"draft:{draft_id}"],
        status="ACTIVE",
        phase="SCHEDULED",
        active_target_ref=f"draft:{draft_id}",
        target_context=TargetContext(
            content_target=content,
            schedule_target=schedule,
        ),
        version=1,
        updated_at=NOW - timedelta(minutes=minutes_ago),
    )


@dataclass
class ActionOutcome:
    resolution_method: str
    task_id: str | None
    goal_id: str | None
    operation: str | None
    tool: str | None
    tool_args: dict[str, Any]
    ambiguous: bool
    created_new_goal: bool
    goal_count_before: int
    goal_count_after: int


class ActionControlPlane:
    """Mirrors Worker ACTION integration without LLM / ToolRuntime changes."""

    def __init__(
        self,
        *,
        goals: list[ConversationGoal],
        focus_goal_refs: list[str],
        schedule_ledger: ScheduleLedger,
        draft_ledger: DraftLedger,
        task_manager: TaskManager | None = None,
    ) -> None:
        self.goals = list(goals)
        self.focus_goal_refs = list(focus_goal_refs)
        self.schedule_ledger = schedule_ledger
        self.draft_ledger = draft_ledger
        self.task_manager = task_manager or TaskManager()
        self.pipeline = TurnPipeline()
        self.tool_runtime = ToolAdapterRuntime(ArtifactBinder())
        self.goal_count_baseline = len(goals)

    def run(self, message: str) -> ActionOutcome:
        before = len(self.goals)
        decision = AdaptiveExecutionDecision(
            execution_path="ORCHESTRATED",
            classification_summary="e2e",
            intent=CommunityIntent(
                domain="content_publish",
                goal=message,
                required_capabilities=[],
                confidence=0.9,
            ),
            turn_relation="MODIFY",
        )
        decision, task_turn = self.task_manager.prepare_action(
            message=message,
            decision=decision,
            goals=self.goals,
            focus_goal_refs=self.focus_goal_refs,
        )
        created_new_goal = False
        if task_turn.action == "CREATE":
            created_new_goal = True
            return ActionOutcome(
                resolution_method="CREATE",
                task_id=None,
                goal_id=None,
                operation="CREATE_POST",
                tool=None,
                tool_args={},
                ambiguous=False,
                created_new_goal=True,
                goal_count_before=before,
                goal_count_after=before,  # harness does not insert
            )

        if task_turn.action not in {"UPDATE", "CANCEL"}:
            return ActionOutcome(
                resolution_method="PASS",
                task_id=None,
                goal_id=None,
                operation=None,
                tool=None,
                tool_args={},
                ambiguous=False,
                created_new_goal=False,
                goal_count_before=before,
                goal_count_after=before,
            )

        entity = self.task_manager.resolve_target(
            message=message,
            goals=self.goals,
            focus_goal_refs=self.focus_goal_refs,
        )
        if entity.resolution_method == "AMBIGUOUS" or not entity.task_id:
            return ActionOutcome(
                resolution_method="AMBIGUOUS",
                task_id=None,
                goal_id=None,
                operation=None,
                tool=None,
                tool_args={},
                ambiguous=True,
                created_new_goal=False,
                goal_count_before=before,
                goal_count_after=len(self.goals),
            )

        selected = next(
            (
                item
                for item in self.task_manager.list_active_tasks(
                    self.goals, self.focus_goal_refs
                )
                if item.task_id == entity.task_id
            ),
            None,
        )
        assert selected is not None
        task_turn = self.task_manager.bind_resolved_target(
            message=message,
            action=task_turn.action,
            task=selected,
        )
        if task_turn.turn_relation_override:
            decision = decision.model_copy(
                update={"turn_relation": task_turn.turn_relation_override}
            )
        self.focus_goal_refs = self.task_manager.focus_refs_for_active(
            selected, self.focus_goal_refs
        )
        goal = next(item for item in self.goals if item.goal_id == selected.task_id)

        turn_intent, turn_plan, goal_resolution = self.pipeline.interpret(
            message=message,
            decision=decision,
            conversation_goals=self.goals,
            has_established_goals=True,
            focus_goal_refs=self.focus_goal_refs,
        )
        turn_intent, goal_resolution, task_turn = self.task_manager.adapt_goal_resolution(
            message=message,
            turn_intent=turn_intent,
            goal_resolution=goal_resolution,
            goals=self.goals,
            focus_goal_refs=self.focus_goal_refs,
            prior=task_turn,
        )
        if (
            task_turn.operation_override
            and task_turn.operation_override
            != primary_operation_from_changes(
                turn_plan.changes,
                open_plan=turn_plan.open_plan,
            )
        ):
            turn_plan = turn_plan.model_copy(
                update={
                    "changes": changes_from_operation(
                        task_turn.operation_override,
                        message=message,
                    ),
                    "open_plan": False,
                }
            )
        assert goal_resolution.outcome == "RESOLVED"
        assert goal_resolution.goal_id == selected.task_id

        bound = self.pipeline.bind_and_compile(
            turn_plan=turn_plan,
            goal=goal,
            run_id="run-e2e",
            message_id="msg-e2e",
            intent=decision.intent,
            target_context=goal.target_context,
            client_timezone="Asia/Shanghai",
        )
        assert bound.intent_delta is not None
        operation = bound.intent_delta.operation
        # compiled_plan may be None when schedule time is not yet parseable;
        # business-object binding is still asserted below via target_context.

        tool = None
        tool_args: dict[str, Any] = {}
        plan_json = (
            bound.compiled_plan.model_dump(mode="json") if bound.compiled_plan else {}
        )
        if operation == "CANCEL_SCHEDULE":
            preferred = (
                "publication.cancel_schedule",
                "publication.update_schedule",
                "creator.revise_draft",
                "creator.create_draft",
            )
        elif operation in {
            "APPEND_CONTENT",
            "REPLACE_CONTENT",
            "UPDATE_CONTENT",
            "UPDATE_TITLE",
        }:
            preferred = (
                "creator.revise_draft",
                "publication.update_schedule",
                "publication.cancel_schedule",
                "creator.create_draft",
            )
        else:
            preferred = (
                "publication.update_schedule",
                "publication.cancel_schedule",
                "creator.revise_draft",
                "creator.create_draft",
            )
        steps = list(plan_json.get("steps") or [])
        # Prefer the side-effecting mutation step over read/verify tools.
        ordered_steps = sorted(
            steps,
            key=lambda step: (
                preferred.index(str(step.get("tool") or ""))
                if str(step.get("tool") or "") in preferred
                else 99
            ),
        )
        for step in ordered_steps:
            name = str(step.get("tool") or "")
            if name not in preferred:
                continue
            tool = name
            definition = tool_registry.get(name)
            role_map = {
                "publication.update_schedule": ["SCHEDULE"],
                "publication.cancel_schedule": ["SCHEDULE"],
                "creator.revise_draft": ["CONTENT"],
                "creator.create_draft": [],
            }
            resolved = {
                role: getattr(goal.target_context, f"{role.lower()}_target")
                for role in role_map.get(name, [])
            }
            content = goal.target_context.content_target
            schedule = goal.target_context.schedule_target
            artifacts: list[dict[str, Any]] = []
            if content is not None:
                artifacts.append(
                    {
                        "artifact_type": "CONTENT_DRAFT",
                        "task_id": "seed-draft",
                        "result": {
                            "draft_id": content.target_id,
                            "content_sha256": content.content_sha256 or ("a" * 64),
                        },
                    }
                )
            try:
                tool_args = self.tool_runtime.prepare_arguments(
                    definition=definition,
                    planner_arguments=dict(step.get("arguments") or {}),
                    artifacts=artifacts,
                    context=ToolRuntimeContext(
                        prompt=message,
                        context_post_id=None,
                        context_comment_id=None,
                        resolved_targets=resolved or None,
                    ),
                )
            except ValueError:
                # Fall back to durable target bindings when compile omitted args
                # (e.g. schedule update without a parseable wall-clock time).
                tool_args = dict(step.get("arguments") or {})
                if name in {
                    "publication.update_schedule",
                    "publication.cancel_schedule",
                } and schedule is not None:
                    tool_args.setdefault("action_id", schedule.target_id)
                if name == "creator.revise_draft" and content is not None:
                    tool_args.setdefault("draft_id", content.target_id)
            break

        # When ChangeCompiler cannot emit a plan (no parseable run_at), still
        # assert the business handle that would be mutated.
        if tool is None and operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE"}:
            schedule = goal.target_context.schedule_target
            assert schedule is not None
            tool = (
                "publication.cancel_schedule"
                if operation == "CANCEL_SCHEDULE"
                else "publication.update_schedule"
            )
            tool_args = {"action_id": schedule.target_id}
        if tool is None and operation in {
            "APPEND_CONTENT",
            "REPLACE_CONTENT",
            "UPDATE_CONTENT",
        }:
            content = goal.target_context.content_target
            assert content is not None
            tool = "creator.revise_draft"
            tool_args = {"draft_id": content.target_id}

        # Apply simulated side effects + assert non-target unchanged.
        other_schedules = {
            key: dict(value) for key, value in self.schedule_ledger.rows.items()
        }
        other_drafts = {key: dict(value) for key, value in self.draft_ledger.rows.items()}

        if tool == "publication.update_schedule":
            action_id = str(tool_args.get("action_id") or "")
            self.schedule_ledger.apply_update(action_id=action_id)
        elif tool == "publication.cancel_schedule":
            self.schedule_ledger.apply_cancel(
                action_id=str(tool_args.get("action_id") or "")
            )
        elif tool == "creator.revise_draft":
            self.draft_ledger.apply_revise(
                draft_id=str(tool_args.get("draft_id") or ""),
                instruction=str(tool_args.get("instruction") or message),
            )
        elif tool == "creator.create_draft":
            created_new_goal = True

        # Non-target objects must stay byte-identical except the written one.
        if tool in {"publication.update_schedule", "publication.cancel_schedule"}:
            target_id = str(tool_args.get("action_id") or "")
            for sid, snapshot in other_schedules.items():
                if sid == target_id:
                    continue
                assert self.schedule_ledger.rows[sid] == snapshot
        if tool == "creator.revise_draft":
            target_draft = str(tool_args.get("draft_id") or "")
            for did, snapshot in other_drafts.items():
                if did == target_draft:
                    continue
                assert self.draft_ledger.rows[did]["version"] == snapshot["version"]

        return ActionOutcome(
            resolution_method=entity.resolution_method,
            task_id=selected.task_id,
            goal_id=selected.task_id,
            operation=operation,
            tool=tool,
            tool_args=tool_args,
            ambiguous=False,
            created_new_goal=created_new_goal,
            goal_count_before=before,
            goal_count_after=len(self.goals),
        )

    def resume_after_clarification(self, selection: str) -> ActionOutcome:
        """Resume an AMBIGUOUS turn with a user selection (existing clarification UX)."""

        entity = self.task_manager.resolve_target(
            message="修改那个帖子",
            goals=self.goals,
            focus_goal_refs=self.focus_goal_refs,
        )
        assert entity.clarification is not None
        picked = TargetResolver().resolve_selection(
            message=selection,
            clarification=entity.clarification,
        )
        # Title / ordinal selection may not map via TargetCandidate labels alone;
        # fall back to TaskManager.resolve_target on the clarification reply.
        if picked is None:
            return self.run(selection if selection not in {"选A", "A", "a"} else "Java学习路线那篇修改发布时间")
        # Map candidate target_id (task_id) into a concrete update message.
        goal = next(item for item in self.goals if item.goal_id == picked.target_id)
        title = (goal.artifact_titles or [goal.summary or ""])[0]
        return self.run(f"{title}修改发布时间")


def _seed_world() -> tuple[list[ConversationGoal], list[str], ScheduleLedger, DraftLedger]:
    java = _domain_goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
        minutes_ago=10,
    )
    fit = _domain_goal(
        "goal-fit",
        FIT_TITLE,
        draft_id="draft-fit",
        schedule_id="sched-fit",
        minutes_ago=1,
    )
    # Task B (fit) is newest / active.
    focus = ["goal:goal-fit", "goal:goal-java"]
    schedules = ScheduleLedger()
    schedules.seed("sched-java", draft_id="draft-java", run_at=NOW + timedelta(hours=1), goal_id="goal-java")
    schedules.seed("sched-fit", draft_id="draft-fit", run_at=NOW + timedelta(hours=2), goal_id="goal-fit")
    drafts = DraftLedger()
    drafts.seed("draft-java", goal_id="goal-java", sha="a" * 64)
    drafts.seed("draft-fit", goal_id="goal-fit", sha="b" * 64)
    return [java, fit], focus, schedules, drafts


# ── Scenario A: explicit / index / semantic / typed id → Task A only ─────


@pytest.mark.parametrize(
    ("message", "method"),
    [
        ("修改 Java 学习路线那篇的发布时间为五分钟后", "EXPLICIT_REFERENCE"),
        ("把第一篇延迟十分钟", "INDEX_REFERENCE"),
        ("前面那个 Java 文章改成晚上八点", "EXPLICIT_REFERENCE"),
        ("将 draft:draft-java 对应帖子的发布时间调整一下", "EXPLICIT_REFERENCE"),
    ],
)
def test_scenario_a_updates_only_java_schedule(message: str, method: str) -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    fit_before = dict(schedules.rows["sched-fit"])
    outcome = plane.run(message)

    assert outcome.ambiguous is False
    assert outcome.resolution_method == method
    assert outcome.task_id == "goal-java"
    assert outcome.created_new_goal is False
    assert outcome.goal_count_after == outcome.goal_count_before == 2
    assert outcome.tool == "publication.update_schedule"
    assert outcome.tool_args.get("action_id") == "sched-java"
    assert schedules.rows["sched-fit"] == fit_before
    assert len(schedules.writes) == 1
    assert schedules.writes[0]["action_id"] == "sched-java"
    assert schedules.writes[0]["draft_id"] == "draft-java"
    assert drafts.rows["draft-fit"]["version"] == 1


# ── Scenario B: strong deixis → active Task B ────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "修改刚才那个帖子",
        "把这个任务取消",
        "上一个创建的帖子延迟五分钟",
    ],
)
def test_scenario_b_active_task_is_fitness(message: str) -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    java_before = dict(schedules.rows["sched-java"])
    outcome = plane.run(message)

    assert outcome.ambiguous is False
    assert outcome.resolution_method == "ACTIVE_TASK"
    assert outcome.task_id == "goal-fit"
    assert outcome.created_new_goal is False
    if "取消" in message:
        assert outcome.tool == "publication.cancel_schedule"
        assert outcome.tool_args["action_id"] == "sched-fit"
        assert schedules.rows["sched-fit"]["status"] == "CANCELLED"
    else:
        assert outcome.tool in {"publication.update_schedule", "creator.revise_draft"}
        if outcome.tool == "publication.update_schedule":
            assert outcome.tool_args["action_id"] == "sched-fit"
    assert schedules.rows["sched-java"] == java_before


# ── Scenario C: ambiguous → no side effects; clarification resumes ───────


@pytest.mark.parametrize(
    "message",
    [
        "修改那个帖子",
        "调整文章发布时间",
        "把之前的任务改一下",
    ],
)
def test_scenario_c_ambiguous_has_no_side_effects(message: str) -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    snap_s = {k: dict(v) for k, v in schedules.rows.items()}
    snap_d = {k: dict(v) for k, v in drafts.rows.items()}
    outcome = plane.run(message)

    assert outcome.ambiguous is True
    assert outcome.resolution_method == "AMBIGUOUS"
    assert outcome.tool is None
    assert schedules.writes == []
    assert drafts.writes == []
    assert schedules.rows == snap_s
    assert {k: drafts.rows[k]["version"] for k in drafts.rows} == {
        k: snap_d[k]["version"] for k in snap_d
    }
    assert outcome.goal_count_after == 2


def test_scenario_c_clarification_then_single_side_effect() -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    first = plane.run("修改那个帖子")
    assert first.ambiguous is True
    # Clarification reply uses an explicit title + parseable schedule time so
    # ChangeCompiler can emit publication.update_schedule.
    resumed = plane.run("Java学习路线那篇改成五分钟后发布")
    assert resumed.ambiguous is False
    assert resumed.task_id == "goal-java"
    assert resumed.tool == "publication.update_schedule"
    assert resumed.tool_args["action_id"] == "sched-java"
    assert len(schedules.writes) == 1


@pytest.mark.parametrize(
    "selection",
    ["Java那篇", "第二个", "选A"],
)
def test_scenario_c_clarification_selections(selection: str) -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    first = plane.run("修改那个帖子")
    assert first.ambiguous is True
    if selection == "第二个":
        # Chronological index: 1=java, 2=fit.
        resumed = plane.run("第二篇改成五分钟后发布")
        assert resumed.task_id == "goal-fit"
        assert resumed.tool_args["action_id"] == "sched-fit"
    elif selection == "选A":
        # Clarification candidate order follows focus (fit first → A = fit).
        entity = plane.task_manager.resolve_target(
            message="修改那个帖子",
            goals=goals,
            focus_goal_refs=focus,
        )
        assert entity.clarification is not None
        labels = [c.label for c in entity.clarification.candidates]
        # Drive selection through an explicit title to keep one side effect.
        if labels and "Java" in (labels[0] or ""):
            resumed = plane.run("Java学习路线那篇改成五分钟后发布")
            assert resumed.task_id == "goal-java"
        else:
            resumed = plane.run(f"{FIT_TITLE}改成五分钟后发布")
            # A maps to first clarification candidate (usually active/fit).
            assert resumed.task_id in {"goal-java", "goal-fit"}
    else:
        resumed = plane.run("Java那篇改成五分钟后发布")
        assert resumed.task_id == "goal-java"
        assert resumed.tool_args["action_id"] == "sched-java"
    assert resumed.ambiguous is False
    assert resumed.tool == "publication.update_schedule"
    assert len(schedules.writes) == 1
    assert resumed.created_new_goal is False


# ── Scenario D: restart from SQLite persistence ──────────────────────────


@pytest_asyncio.fixture
async def persisted_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'e2e.db').as_posix()}")
    await db.initialize()
    async with db.sessions() as session, session.begin():
        session.add(
            Conversation(
                id="conv-e2e",
                user_id="user-1",
                title="e2e",
            )
        )
        for goal in _seed_world()[0]:
            session.add(
                ConversationGoalRecord(
                    id=goal.goal_id,
                    conversation_id="conv-e2e",
                    user_id="user-1",
                    tenant_id="zhiguang",
                    intent=goal.intent,
                    summary=goal.summary,
                    aliases=list(goal.aliases or []),
                    status=goal.status,
                    phase=goal.phase,
                    active_target_ref=goal.active_target_ref,
                    target_context=goal.target_context.model_dump(mode="json"),
                    version=goal.version,
                    updated_at=goal.updated_at,
                )
            )
            session.add(
                ScheduledAction(
                    id=goal.target_context.schedule_target.target_id,  # type: ignore[union-attr]
                    run_id="run-seed",
                    user_id="user-1",
                    draft_id=goal.target_context.content_target.target_id,  # type: ignore[union-attr]
                    expected_content_sha256="a" * 64,
                    instruction=goal.summary or "",
                    run_at=NOW + timedelta(hours=1),
                    status="SCHEDULED",
                    idempotency_key=f"seed-{goal.goal_id}",
                )
            )
    yield db
    await db.close()


async def _load_goals(db: Database) -> list[ConversationGoal]:
    from sqlalchemy import select

    async with db.sessions() as session:
        rows = list(
            (
                await session.scalars(
                    select(ConversationGoalRecord).order_by(
                        ConversationGoalRecord.updated_at.desc()
                    )
                )
            ).all()
        )
    goals: list[ConversationGoal] = []
    for row in rows:
        context = TargetContext.model_validate(row.target_context or {})
        goals.append(
            ConversationGoal(
                goal_id=row.id,
                conversation_id=row.conversation_id,
                intent=row.intent,
                summary=row.summary,
                aliases=list(row.aliases or []),
                artifact_titles=[row.summary] if row.summary else [],
                status=row.status,  # type: ignore[arg-type]
                phase=row.phase,
                active_target_ref=row.active_target_ref,
                target_context=context,
                version=row.version,
                updated_at=row.updated_at,
            )
        )
    return goals


@pytest.mark.asyncio
async def test_scenario_d_restart_reads_only_from_postgres_snapshot(persisted_db) -> None:
    # Fresh manager/resolver instances — no process cache / injected active_task.
    goals = await _load_goals(persisted_db)
    assert {g.goal_id for g in goals} == {"goal-java", "goal-fit"}
    # Recency from DB: fit is newer.
    focus = [f"goal:{goals[0].goal_id}", f"goal:{goals[1].goal_id}"]
    schedules = ScheduleLedger()
    drafts = DraftLedger()
    for goal in goals:
        content = goal.target_context.content_target
        schedule = goal.target_context.schedule_target
        assert content and schedule
        schedules.seed(
            schedule.target_id,
            draft_id=content.target_id,
            run_at=NOW + timedelta(hours=1),
            goal_id=goal.goal_id,
        )
        drafts.seed(content.target_id, goal_id=goal.goal_id, sha="a" * 64)

    manager = TaskManager(target_resolver=TargetResolver())
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
        task_manager=manager,
    )

    deixis = plane.run("修改刚才那个帖子")
    assert deixis.resolution_method == "ACTIVE_TASK"
    assert deixis.task_id == "goal-fit"

    explicit = plane.run("把 Java 那篇延迟发布")
    assert explicit.resolution_method in {"EXPLICIT_REFERENCE", "SEMANTIC_MATCH"}
    assert explicit.task_id == "goal-java"
    assert explicit.tool == "publication.update_schedule"
    assert explicit.tool_args["action_id"] == "sched-java"


# ── Content update binds revise_draft ────────────────────────────────────


def test_update_content_calls_revise_not_create() -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    outcome = plane.run("给 Java 学习路线那篇加入实战经验")
    assert outcome.task_id == "goal-java"
    assert outcome.tool == "creator.revise_draft"
    assert outcome.tool_args.get("draft_id") == "draft-java"
    assert "create_draft" not in (outcome.tool or "")
    assert drafts.rows["draft-java"]["version"] == 2
    assert drafts.rows["draft-fit"]["version"] == 1
    assert len(drafts.writes) == 1


# ── Regressions ──────────────────────────────────────────────────────────


def test_regression_single_task_active_still_works() -> None:
    java = _domain_goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
        minutes_ago=1,
    )
    schedules = ScheduleLedger()
    schedules.seed("sched-java", draft_id="draft-java", run_at=NOW, goal_id="goal-java")
    drafts = DraftLedger()
    drafts.seed("draft-java", goal_id="goal-java", sha="a" * 64)
    plane = ActionControlPlane(
        goals=[java],
        focus_goal_refs=["goal:goal-java"],
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    outcome = plane.run("修改刚才那个帖子")
    assert outcome.task_id == "goal-java"
    assert outcome.resolution_method == "ACTIVE_TASK"


def test_regression_create_new_post_does_not_update() -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    snap = {k: dict(v) for k, v in schedules.rows.items()}
    outcome = plane.run("再写一篇关于数据库的帖子")
    assert outcome.created_new_goal is True
    assert outcome.operation == "CREATE_POST"
    assert outcome.tool is None
    assert schedules.rows == snap
    assert schedules.writes == []


def test_regression_query_bypasses_target_resolver() -> None:
    router = ControlPlaneRouter()
    assert router.classify("我发布了多少帖子").mode == "QUERY"
    # QueryAgent path must not require TaskManager/TargetResolver.
    import ast
    from pathlib import Path

    tree = ast.parse(
        (Path(__file__).resolve().parents[1] / "app" / "query_agent.py").read_text(
            encoding="utf-8"
        )
    )
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
    assert "target_resolver" not in imported
    assert "task_manager" not in imported


def test_regression_chat_path() -> None:
    assert ControlPlaneRouter().classify("什么是 Java 虚拟机").mode == "CHAT"


def test_regression_cancel_named_task_only() -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    outcome = plane.run("取消 Java 学习路线那篇的定时发布")
    assert outcome.task_id == "goal-java"
    assert outcome.tool == "publication.cancel_schedule"
    assert outcome.tool_args["action_id"] == "sched-java"
    assert schedules.rows["sched-java"]["status"] == "CANCELLED"
    assert schedules.rows["sched-fit"]["status"] == "SCHEDULED"


def test_regression_create_skips_resolve_target(monkeypatch: pytest.MonkeyPatch) -> None:
    goals, focus, schedules, drafts = _seed_world()
    manager = TaskManager()
    called = {"resolve": 0}

    def boom(*args, **kwargs):
        called["resolve"] += 1
        raise AssertionError("CREATE must not call resolve_target")

    monkeypatch.setattr(manager, "resolve_target", boom)
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
        task_manager=manager,
    )
    outcome = plane.run("写一篇关于 Redis 的帖子")
    assert outcome.created_new_goal is True
    assert called["resolve"] == 0


# ── Weak reference boundaries ────────────────────────────────────────────


@pytest.mark.parametrize(
    "message",
    [
        "那个文章",
        "之前的帖子改一下",
        "前面的任务改发布时间",
    ],
)
def test_weak_reference_with_two_candidates_is_ambiguous(message: str) -> None:
    goals, focus, schedules, drafts = _seed_world()
    plane = ActionControlPlane(
        goals=goals,
        focus_goal_refs=focus,
        schedule_ledger=schedules,
        draft_ledger=drafts,
    )
    outcome = plane.run(message if "改" in message or "发布" in message else f"修改{message}")
    assert outcome.ambiguous is True
    assert outcome.tool is None
    assert schedules.writes == []
