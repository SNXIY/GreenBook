"""Phase 3.7: TemporalResolver + ChangeCompiler gates + durable integration.

Exercises the production control-plane chain used by Worker:
  TaskManager → TargetResolver → TemporalResolver → ChangeCompiler
  → publication.update_schedule arguments → ScheduledAction / SideEffect

Also simulates HTTP message → Run(QUEUED) → claim → COMPLETED with a stub
tool transport, without calling external MCP services.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.change_compiler import ChangeCompiler
from app.database import (
    Conversation,
    ConversationGoal as ConversationGoalRecord,
    Database,
    Message,
    Run,
    ScheduledAction,
    SideEffect,
    utc_now,
)
from app.domain import (
    AdaptiveExecutionDecision,
    CommunityIntent,
    ConversationGoal,
    TargetBinding,
    TargetContext,
)
from app.task_manager import TaskManager
from app.temporal_resolver import (
    resolve_schedule_time,
    run_at_utc_isoformat,
)
from app.turn_plan import Change, TurnPlan

SH = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 4, 15, 30, tzinfo=SH)
EXISTING_JAVA = datetime(2026, 8, 5, 8, 0, tzinfo=SH)
EXISTING_FIT = datetime(2026, 8, 5, 10, 0, tzinfo=SH)
JAVA_TITLE = "如何高效学好 Java：一份实用的学习路线图"
FIT_TITLE = "科学减肥：从饮食、运动到生活习惯的完整指南"


def _goal(
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
        conversation_id="conv-temporal",
        intent="CONTENT_PUBLISH",
        summary=title,
        artifact_titles=[title],
        aliases=[title],
        explicit_refs=[draft_id, schedule_id, f"draft:{draft_id}"],
        status="ACTIVE",
        phase="SCHEDULED",
        active_target_ref=f"draft:{draft_id}",
        target_context=TargetContext(content_target=content, schedule_target=schedule),
        version=1,
        updated_at=NOW - timedelta(minutes=minutes_ago),
    )


def _intent(message: str) -> CommunityIntent:
    return CommunityIntent(
        domain="content_publish",
        goal=message,
        required_capabilities=["schedule_publish"],
        confidence=0.9,
    )


def test_change_compiler_delay_emits_utc_run_at() -> None:
    goal = _goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
        minutes_ago=10,
    )
    plan = TurnPlan(
        turn_relation="MODIFY",
        changes=[
            Change(
                role="SCHEDULE",
                op="UPDATE",
                payload={"schedule_request": "延迟十分钟", "message": "延迟十分钟"},
            )
        ],
        raw_message="延迟十分钟",
    )
    compiled = ChangeCompiler().compile(
        turn_plan=plan,
        target_context=goal.target_context,
        intent=_intent("延迟十分钟"),
        client_timezone="Asia/Shanghai",
        current_time=NOW,
        existing_run_at=EXISTING_JAVA,
    )
    assert compiled is not None
    update = next(
        step for step in compiled.steps if step.tool == "publication.update_schedule"
    )
    assert update.arguments["run_at"] == "2026-08-05T00:10:00Z"
    assert "delay_seconds" not in update.arguments


def test_change_compiler_incomplete_emits_no_update_step() -> None:
    goal = _goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
        minutes_ago=1,
    )
    plan = TurnPlan(
        turn_relation="MODIFY",
        changes=[
            Change(
                role="SCHEDULE",
                op="UPDATE",
                payload={"message": "调整一下发布时间"},
            )
        ],
        raw_message="调整一下发布时间",
    )
    compiled = ChangeCompiler().compile(
        turn_plan=plan,
        target_context=goal.target_context,
        intent=_intent("调整一下发布时间"),
        client_timezone="Asia/Shanghai",
        current_time=NOW,
        existing_run_at=EXISTING_JAVA,
    )
    assert compiled is None


def test_stamped_run_at_does_not_drift_on_recompile() -> None:
    goal = _goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
        minutes_ago=1,
    )
    stamped = run_at_utc_isoformat(EXISTING_JAVA + timedelta(minutes=10))
    plan = TurnPlan(
        turn_relation="MODIFY",
        changes=[
            Change(
                role="SCHEDULE",
                op="UPDATE",
                payload={
                    "schedule_request": "延迟十分钟",
                    "run_at": stamped,
                    "mutation": "UPDATE_SCHEDULE_TIME",
                },
            )
        ],
        raw_message="延迟十分钟",
    )
    later = NOW + timedelta(minutes=5)
    compiled = ChangeCompiler().compile(
        turn_plan=plan,
        target_context=goal.target_context,
        intent=_intent("延迟十分钟"),
        client_timezone="Asia/Shanghai",
        current_time=later,
        existing_run_at=EXISTING_JAVA,
    )
    assert compiled is not None
    update = next(
        step for step in compiled.steps if step.tool == "publication.update_schedule"
    )
    assert update.arguments["run_at"] == stamped


def test_control_plane_binds_java_and_solidifies_existing_plus_ten() -> None:
    java = _goal(
        "goal-java",
        JAVA_TITLE,
        draft_id="draft-java",
        schedule_id="sched-java",
        minutes_ago=10,
    )
    fit = _goal(
        "goal-fit",
        FIT_TITLE,
        draft_id="draft-fit",
        schedule_id="sched-fit",
        minutes_ago=1,
    )
    goals = [java, fit]
    focus = ["goal:goal-fit", "goal:goal-java"]
    message = "把 Java 学习路线那篇延迟十分钟"
    manager = TaskManager()
    decision = AdaptiveExecutionDecision(
        execution_path="ORCHESTRATED",
        classification_summary="e2e",
        intent=_intent(message),
        turn_relation="MODIFY",
    )
    decision, task_turn = manager.prepare_action(
        message=message,
        decision=decision,
        goals=goals,
        focus_goal_refs=focus,
    )
    assert task_turn.action == "UPDATE"
    entity = manager.resolve_target(message=message, goals=goals, focus_goal_refs=focus)
    assert entity.resolution_method == "EXPLICIT_REFERENCE"
    assert entity.task_id == "goal-java"
    temporal = resolve_schedule_time(
        message=message,
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING_JAVA,
    )
    assert temporal.mode == "RELATIVE_TO_EXISTING"
    assert temporal.run_at == EXISTING_JAVA + timedelta(minutes=10)
    plan = TurnPlan(
        turn_relation="MODIFY",
        goal_ref="goal:goal-java",
        changes=[
            Change(
                role="SCHEDULE",
                op="UPDATE",
                payload={
                    "schedule_request": message,
                    "run_at": run_at_utc_isoformat(temporal.run_at),
                    "mutation": "UPDATE_SCHEDULE_TIME",
                },
            )
        ],
        raw_message=message,
    )
    compiled = ChangeCompiler().compile(
        turn_plan=plan,
        target_context=java.target_context,
        intent=decision.intent,
        client_timezone="Asia/Shanghai",
        current_time=NOW,
        existing_run_at=EXISTING_JAVA,
    )
    assert compiled is not None
    update = next(
        step for step in compiled.steps if step.tool == "publication.update_schedule"
    )
    assert update.arguments["run_at"] == "2026-08-05T00:10:00Z"


@pytest_asyncio.fixture
async def temporal_db(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{(tmp_path / 'temporal.db').as_posix()}")
    await db.initialize()
    async with db.sessions() as session, session.begin():
        session.add(
            Conversation(id="conv-temporal", user_id="user-1", title="temporal")
        )
        for goal_id, title, draft_id, schedule_id, run_at, minutes_ago in [
            (
                "goal-java",
                JAVA_TITLE,
                "draft-java",
                "sched-java",
                EXISTING_JAVA.astimezone(timezone.utc),
                10,
            ),
            (
                "goal-fit",
                FIT_TITLE,
                "draft-fit",
                "sched-fit",
                EXISTING_FIT.astimezone(timezone.utc),
                1,
            ),
        ]:
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
            session.add(
                ConversationGoalRecord(
                    id=goal_id,
                    conversation_id="conv-temporal",
                    user_id="user-1",
                    tenant_id="zhiguang",
                    intent="CONTENT_PUBLISH",
                    summary=title,
                    aliases=[title],
                    status="ACTIVE",
                    phase="SCHEDULED",
                    active_target_ref=f"draft:{draft_id}",
                    target_context=TargetContext(
                        content_target=content,
                        schedule_target=schedule,
                    ).model_dump(mode="json"),
                    version=1,
                    updated_at=(NOW - timedelta(minutes=minutes_ago)).astimezone(
                        timezone.utc
                    ),
                )
            )
            session.add(
                ScheduledAction(
                    id=schedule_id,
                    run_id="run-seed",
                    user_id="user-1",
                    draft_id=draft_id,
                    expected_content_sha256="a" * 64,
                    instruction=title,
                    run_at=run_at,
                    status="SCHEDULED",
                    idempotency_key=f"seed-{goal_id}",
                )
            )
    yield db
    await db.close()


@pytest.mark.asyncio
async def test_http_like_run_updates_java_schedule_once(temporal_db: Database) -> None:
    """Simulate HTTP message → Run(QUEUED) → control plane → stub tool → COMPLETED."""

    message_text = "把 Java 学习路线那篇延迟十分钟"
    message_created = NOW.astimezone(timezone.utc)

    async with temporal_db.sessions() as session, session.begin():
        fit_before = await session.get(ScheduledAction, "sched-fit")
        java_before = await session.get(ScheduledAction, "sched-java")
        assert fit_before is not None and java_before is not None
        fit_snapshot = fit_before.run_at
        java_snapshot = java_before.run_at
        run = Run(
            id="run-temporal-1",
            conversation_id="conv-temporal",
            user_id="user-1",
            tenant_id="zhiguang",
            principal_role="user",
            prompt=message_text,
            client_timezone="Asia/Shanghai",
            status="QUEUED",
            created_at=message_created,
        )
        session.add(run)
        session.add(
            Message(
                conversation_id="conv-temporal",
                role="user",
                content=message_text,
                parts=[],
                run_id=run.id,
                created_at=message_created,
            )
        )
        await session.flush()
        run.checkpoint = {**(run.checkpoint or {}), "message_id": None}
        # Persist message id into checkpoint once available.
        msg = (
            await session.scalars(
                select(Message).where(Message.run_id == run.id).limit(1)
            )
        ).first()
        assert msg is not None
        run.checkpoint = {**(run.checkpoint or {}), "message_id": msg.id}

    # --- claim ---
    async with temporal_db.sessions() as session, session.begin():
        run = await session.get(Run, "run-temporal-1", with_for_update=True)
        assert run is not None
        run.status = "RUNNING"
        run.started_at = utc_now()
        run.attempts = 1

    # --- control plane (Worker-equivalent, no external tools) ---
    async with temporal_db.sessions() as session:
        goal_rows = list(
            (
                await session.scalars(select(ConversationGoalRecord))
            ).all()
        )
        java_action = await session.get(ScheduledAction, "sched-java")
        assert java_action is not None
        existing = java_action.run_at
        if existing.tzinfo is None:
            existing = existing.replace(tzinfo=timezone.utc)

    goals: list[ConversationGoal] = []
    for row in goal_rows:
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
                target_context=TargetContext.model_validate(row.target_context or {}),
                version=row.version,
                updated_at=row.updated_at,
            )
        )
    focus = ["goal:goal-fit", "goal:goal-java"]
    manager = TaskManager()
    entity = manager.resolve_target(
        message=message_text, goals=goals, focus_goal_refs=focus
    )
    assert entity.task_id == "goal-java"
    java = next(item for item in goals if item.goal_id == "goal-java")
    temporal = resolve_schedule_time(
        message=message_text,
        current_time=message_created.astimezone(SH),
        timezone="Asia/Shanghai",
        existing_run_at=existing.astimezone(SH),
    )
    assert temporal.run_at == EXISTING_JAVA + timedelta(minutes=10)
    stamped = run_at_utc_isoformat(temporal.run_at)
    compiled = ChangeCompiler().compile(
        turn_plan=TurnPlan(
            turn_relation="MODIFY",
            goal_ref="goal:goal-java",
            changes=[
                Change(
                    role="SCHEDULE",
                    op="UPDATE",
                    payload={
                        "schedule_request": message_text,
                        "run_at": stamped,
                        "mutation": "UPDATE_SCHEDULE_TIME",
                    },
                )
            ],
            raw_message=message_text,
        ),
        target_context=java.target_context,
        intent=_intent(message_text),
        client_timezone="Asia/Shanghai",
        current_time=message_created.astimezone(SH),
        existing_run_at=existing.astimezone(SH),
    )
    assert compiled is not None
    update = next(
        step for step in compiled.steps if step.tool == "publication.update_schedule"
    )
    tool_args: dict[str, Any] = {
        "action_id": java.target_context.schedule_target.target_id,  # type: ignore[union-attr]
        "run_at": update.arguments["run_at"],
    }
    assert tool_args["action_id"] == "sched-java"
    assert tool_args["run_at"] == "2026-08-05T00:10:00Z"

    # --- stub tool transport + ledger ---
    async with temporal_db.sessions() as session, session.begin():
        action = await session.get(ScheduledAction, "sched-java", with_for_update=True)
        fit = await session.get(ScheduledAction, "sched-fit")
        run = await session.get(Run, "run-temporal-1", with_for_update=True)
        assert action is not None and fit is not None and run is not None
        new_run_at = datetime.fromisoformat(
            str(tool_args["run_at"]).replace("Z", "+00:00")
        ).astimezone(timezone.utc)
        action.run_at = new_run_at
        session.add(
            SideEffect(
                id="se-temporal-1",
                run_id=run.id,
                step_ordinal=1,
                tool_name="publication.update_schedule",
                operation_key=f"update-schedule:{action.id}:{tool_args['run_at']}",
                status="SUCCEEDED",
                request_hash="hash-1",
                resource_id=action.id,
                result={"action_id": action.id, "run_at": tool_args["run_at"]},
            )
        )
        run.status = "COMPLETED"
        run.final_response = (
            f"已将《{JAVA_TITLE}》的发布时间调整为 "
            f"{temporal.run_at.astimezone(SH).strftime('%Y年%m月%d日%H:%M')}（北京时间）。"
        )
        run.completed_at = utc_now()

    async with temporal_db.sessions() as session:
        java_after = await session.get(ScheduledAction, "sched-java")
        fit_after = await session.get(ScheduledAction, "sched-fit")
        run_after = await session.get(Run, "run-temporal-1")
        effects = list(
            (
                await session.scalars(
                    select(SideEffect).where(SideEffect.run_id == "run-temporal-1")
                )
            ).all()
        )
        goals_count = len(
            list((await session.scalars(select(ConversationGoalRecord))).all())
        )

    assert java_after is not None and fit_after is not None and run_after is not None
    expected_java = (EXISTING_JAVA + timedelta(minutes=10)).astimezone(timezone.utc)
    actual_java = java_after.run_at
    if actual_java.tzinfo is None:
        actual_java = actual_java.replace(tzinfo=timezone.utc)
    actual_fit = fit_after.run_at
    if actual_fit is not None and actual_fit.tzinfo is None:
        actual_fit = actual_fit.replace(tzinfo=timezone.utc)
    fit_cmp = fit_snapshot
    if fit_cmp is not None and fit_cmp.tzinfo is None:
        fit_cmp = fit_cmp.replace(tzinfo=timezone.utc)
    assert actual_java == expected_java
    assert actual_fit == fit_cmp
    assert java_snapshot != java_after.run_at
    assert run_after.status == "COMPLETED"
    assert "北京时间" in (run_after.final_response or "")
    assert len(effects) == 1
    assert effects[0].tool_name == "publication.update_schedule"
    assert goals_count == 2


@pytest.mark.asyncio
async def test_incomplete_schedule_leaves_db_unchanged(temporal_db: Database) -> None:
    message_text = "调整一下发布时间"
    async with temporal_db.sessions() as session, session.begin():
        java_before = await session.get(ScheduledAction, "sched-java")
        assert java_before is not None
        before = java_before.run_at
        run = Run(
            id="run-temporal-2",
            conversation_id="conv-temporal",
            user_id="user-1",
            tenant_id="zhiguang",
            principal_role="user",
            prompt=message_text,
            client_timezone="Asia/Shanghai",
            status="QUEUED",
        )
        session.add(run)

    temporal = resolve_schedule_time(
        message=message_text,
        current_time=NOW,
        timezone="Asia/Shanghai",
        existing_run_at=EXISTING_JAVA,
    )
    assert temporal.mode == "AMBIGUOUS"
    compiled = ChangeCompiler().compile(
        turn_plan=TurnPlan(
            turn_relation="MODIFY",
            changes=[
                Change(role="SCHEDULE", op="UPDATE", payload={"message": message_text})
            ],
            raw_message=message_text,
        ),
        target_context=_goal(
            "goal-java",
            JAVA_TITLE,
            draft_id="draft-java",
            schedule_id="sched-java",
            minutes_ago=1,
        ).target_context,
        intent=_intent(message_text),
        client_timezone="Asia/Shanghai",
        current_time=NOW,
        existing_run_at=EXISTING_JAVA,
    )
    assert compiled is None

    async with temporal_db.sessions() as session, session.begin():
        run = await session.get(Run, "run-temporal-2", with_for_update=True)
        java = await session.get(ScheduledAction, "sched-java")
        assert run is not None and java is not None
        run.status = "WAITING_CLARIFICATION"
        run.final_response = "请提供具体的新发布时间"
        assert java.run_at == before
        effects = list(
            (
                await session.scalars(
                    select(SideEffect).where(SideEffect.run_id == "run-temporal-2")
                )
            ).all()
        )
        assert effects == []


@pytest.mark.parametrize(
    ("message", "seconds"),
    [
        ("schedule it in five minutes", 300),
        ("publish it five minutes from now", 300),
        ("schedule it after two hours", 7_200),
    ],
)
def test_temporal_resolver_accepts_english_relative_time(
    message: str, seconds: int
) -> None:
    resolved = resolve_schedule_time(
        message=message,
        current_time=NOW,
        timezone="Asia/Shanghai",
    )
    assert resolved.mode == "RELATIVE_TO_NOW"
    assert resolved.offset_seconds == seconds
