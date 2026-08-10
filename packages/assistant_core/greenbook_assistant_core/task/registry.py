"""TaskRegistry — CRUD + simple matching for Tasks within a Conversation.

Phase 1: create, find, list, basic label matching.
Phase 2: TaskIntent persistence.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    ArtifactRef,
    Task,
    TaskExecutionRef,
    TaskGoal,
    TaskIntent,
    TaskResourceRef,
    TaskStatus,
)

# ── DB tables ────────────────────────────────────────────────────────

_tasks = sa.Table(
    "assistant_tasks",
    sa.MetaData(),
    sa.Column("task_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("goal", sa.Text, default=""),
    sa.Column("goal_category", sa.String(64), default=""),
    sa.Column("goal_summary", sa.String(500)),
    sa.Column("status", sa.String(32), default="READY"),
    sa.Column("phase", sa.String(64)),
    sa.Column("artifacts", JSONB, default=list),
    sa.Column("depends_on", JSONB, default=list),
    sa.Column("goals", JSONB, default=list),
    sa.Column("execution_refs", JSONB, default=list),
    sa.Column("resource_index", JSONB, default=list),
    sa.Column("last_action", sa.String(64)),
    sa.Column("action_history", JSONB, default=list),
    sa.Column("last_error", sa.Text),
    sa.Column("retry_count", sa.Integer, default=0),
    sa.Column("max_retries", sa.Integer, default=3),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
    sa.Column("updated_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
)

_task_intents = sa.Table(
    "assistant_task_intents",
    sa.MetaData(),
    sa.Column("intent_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    sa.Column("conversation_id", UUID(as_uuid=True), nullable=False),
    sa.Column("run_id", UUID(as_uuid=True)),
    sa.Column("task_id", UUID(as_uuid=True)),
    sa.Column("relation", sa.String(32), nullable=False),
    sa.Column("goal", sa.Text, default=""),
    sa.Column("goal_category", sa.String(64), default=""),
    sa.Column("target_task_id", UUID(as_uuid=True)),
    sa.Column("target_task_hint", sa.String(200)),
    sa.Column("requirements", JSONB, default=list),
    sa.Column("constraints", JSONB, default=list),
    sa.Column("confidence", sa.Float, default=0.0),
    sa.Column("source", sa.String(8), default="L1"),
    sa.Column("intent_json", JSONB),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)

_artifacts = sa.Table(
    "assistant_artifacts",
    sa.MetaData(),
    sa.Column("artifact_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("task_id", UUID(as_uuid=True), nullable=False),
    sa.Column("step_id", sa.String(128), default=""),
    sa.Column("artifact_type", sa.String(64), nullable=False),
    sa.Column("resource_id", sa.String(128)),
    sa.Column("resource_kind", sa.String(32)),
    sa.Column("summary", sa.String(500)),
    sa.Column("content_ref", JSONB),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)


class TaskIntentRepository:
    """Persistence for assistant_task_intents."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert(
        self,
        intent: TaskIntent,
        *,
        conversation_id: str,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        intent_id = uuid.uuid4()
        values = {
            "intent_id": intent_id,
            "conversation_id": uuid.UUID(conversation_id),
            "run_id": uuid.UUID(run_id) if run_id else None,
            "task_id": uuid.UUID(task_id) if task_id else None,
            "relation": intent.relation,
            "goal": intent.goal,
            "goal_category": intent.goal_category,
            "target_task_id": uuid.UUID(intent.target_task_id) if intent.target_task_id else None,
            "target_task_hint": intent.target_task_hint,
            "requirements": intent.requirements,
            "constraints": intent.constraints,
            "confidence": intent.confidence,
            "source": intent.source,
            "intent_json": intent.model_dump(mode="json"),
            "version": 1,
            "created_at": datetime.now(UTC),
        }
        await self._session.execute(sa.insert(_task_intents).values(**values))
        await self._session.commit()
        return str(intent_id)

    async def find_by_run(self, run_id: str) -> TaskIntent | None:
        row = await self._session.execute(
            sa.select(_task_intents).where(
                _task_intents.c.run_id == uuid.UUID(run_id)
            )
        )
        result = row.first()
        if result is None:
            return None
        d = dict(result._mapping)
        return TaskIntent(
            relation=d.get("relation", "NEW_TASK"),
            goal=d.get("goal", ""),
            goal_category=d.get("goal_category", "QUERY_INFO"),
            target_task_id=str(d["target_task_id"]) if d.get("target_task_id") else None,
            target_task_hint=d.get("target_task_hint"),
            requirements=list(d.get("requirements") or []),
            constraints=list(d.get("constraints") or []),
            confidence=float(d.get("confidence", 0.0)),
            source=str(d.get("source", "L1")),
        )


class TaskRepository:
    """Low-level DB access for assistant_tasks / assistant_artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_tables(self) -> None:
        async with self._session.bind.begin() as conn:
            await conn.run_sync(_tasks.metadata.create_all, checkfirst=True)
            await conn.run_sync(_task_intents.metadata.create_all, checkfirst=True)
            # Phase15-B adds a task read-model projection.  Keep this additive
            # and idempotent so existing Phase15-A databases remain usable.
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS goals JSONB"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS execution_refs JSONB"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS resource_index JSONB"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS last_action VARCHAR(64)"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS action_history JSONB"
            )

    async def insert(self, task: Task) -> Task:
        values = {
            "task_id": uuid.UUID(task.task_id),
            "conversation_id": uuid.UUID(task.conversation_id),
            "user_id": task.user_id,
            "tenant_id": task.tenant_id,
            "goal": task.goal,
            "goal_category": task.goal_category,
            "goal_summary": task.goal_summary,
            "status": task.status.value,
            "phase": task.phase,
            "artifacts": [a.model_dump(mode="json") for a in task.artifacts],
            "depends_on": task.depends_on,
            "goals": [g.model_dump(mode="json") for g in task.goals],
            "execution_refs": [e.model_dump(mode="json") for e in task.execution_refs],
            "resource_index": [r.model_dump(mode="json") for r in task.resource_index],
            "last_action": task.last_action,
            "action_history": task.action_history,
            "version": task.version,
            "created_at": datetime.fromisoformat(task.created_at),
            "updated_at": datetime.fromisoformat(task.updated_at),
        }
        await self._session.execute(sa.insert(_tasks).values(**values))
        await self._session.commit()
        return task

    async def find_by_id(self, task_id: str) -> Task | None:
        row = await self._session.execute(
            sa.select(_tasks).where(_tasks.c.task_id == uuid.UUID(task_id))
        )
        result = row.first()
        return self._row_to_task(result) if result else None

    async def find_by_conversation(
        self, conversation_id: str, *, status: TaskStatus | None = None
    ) -> list[Task]:
        stmt = sa.select(_tasks).where(
            _tasks.c.conversation_id == uuid.UUID(conversation_id)
        )
        if status:
            stmt = stmt.where(_tasks.c.status == status.value)
        stmt = stmt.order_by(_tasks.c.updated_at.desc())
        rows = await self._session.execute(stmt)
        return [self._row_to_task(r) for r in rows.all() if r]

    async def update(self, task_id: str, **fields: Any) -> Task | None:
        existing = await self.find_by_id(task_id)
        if existing is None:
            return None
        version = existing.version
        values = {**fields, "version": version + 1,
                  "updated_at": datetime.now(UTC)}
        if "status" in values and isinstance(values["status"], TaskStatus):
            values["status"] = values["status"].value
        await self._session.execute(
            sa.update(_tasks)
            .where(sa.and_(
                _tasks.c.task_id == uuid.UUID(task_id),
                _tasks.c.version == version,
            ))
            .values(**values)
        )
        await self._session.commit()
        return await self.find_by_id(task_id)

    @staticmethod
    def _row_to_task(row: Any) -> Task | None:
        if row is None:
            return None
        d = dict(row._mapping)
        return Task(
            task_id=str(d["task_id"]),
            conversation_id=str(d["conversation_id"]),
            user_id=str(d.get("user_id", "")),
            tenant_id=str(d.get("tenant_id", "")),
            goal=str(d.get("goal", "")),
            goal_category=str(d.get("goal_category", "")),
            goal_summary=d.get("goal_summary"),
            status=TaskStatus(d.get("status", "READY")),
            phase=d.get("phase"),
            artifacts=[
                ArtifactRef(**a) for a in (d.get("artifacts") or [])
            ],
            depends_on=list(d.get("depends_on") or []),
            goals=[TaskGoal(**g) for g in (d.get("goals") or [])],
            execution_refs=[
                TaskExecutionRef(**e) for e in (d.get("execution_refs") or [])
            ],
            resource_index=[
                TaskResourceRef(**r) for r in (d.get("resource_index") or [])
            ],
            last_action=d.get("last_action"),
            action_history=list(d.get("action_history") or []),
            last_error=d.get("last_error"),
            retry_count=int(d.get("retry_count", 0)),
            max_retries=int(d.get("max_retries", 3)),
            version=int(d.get("version", 1)),
            created_at=d["created_at"].isoformat() if d.get("created_at") else "",
            updated_at=d["updated_at"].isoformat() if d.get("updated_at") else "",
            completed_at=d["completed_at"].isoformat() if d.get("completed_at") else None,
        )


class TaskRegistry:
    """Business-logic layer over TaskRepository.

    Phase 1 responsibilities:
      - Create a Task when a run performs actionable work (tool_rounds > 0).
      - Find the "most recent" task for a conversation (simple recency match).
      - List all tasks in a conversation.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._repo = TaskRepository(session)

    async def ensure_tables(self) -> None:
        await self._repo.ensure_tables()

    # ── factory ──

    async def create_task(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        task_id: str | None = None,
        goal: str = "",
        goal_category: str = "",
        goal_summary: str | None = None,
        phase: str | None = None,
        status: TaskStatus = TaskStatus.COMPLETED,
    ) -> Task:
        task = Task(
            task_id=task_id or str(uuid.uuid4()),
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            goal=goal,
            goal_category=goal_category,
            goal_summary=goal_summary,
            phase=phase,
            status=status,
        )
        return await self._repo.insert(task)

    # ── queries ──

    async def get_task(self, task_id: str) -> Task | None:
        return await self._repo.find_by_id(task_id)

    async def list_tasks(
        self, conversation_id: str, *, status: TaskStatus | None = None
    ) -> list[Task]:
        return await self._repo.find_by_conversation(conversation_id, status=status)

    async def update_task(self, task_id: str, **fields: Any) -> Task | None:
        return await self._repo.update(task_id, **fields)

    async def get_most_recent(self, conversation_id: str) -> Task | None:
        tasks = await self.list_tasks(conversation_id)
        return tasks[0] if tasks else None

    # ── Phase 2: TaskIntent persistence ──

    @property
    def intents(self) -> TaskIntentRepository:
        return TaskIntentRepository(self._repo._session)

    async def save_intent(
        self,
        intent: TaskIntent,
        *,
        conversation_id: str,
        run_id: str | None = None,
        task_id: str | None = None,
    ) -> str:
        return await self.intents.insert(
            intent,
            conversation_id=conversation_id,
            run_id=run_id,
            task_id=task_id,
        )

    # ── matching (simple recency for Phase 1, enhanced in Phase 2) ──

    async def resolve_task(
        self, conversation_id: str, hint: str | None = None
    ) -> Task | None:
        """Find the task the user is most likely referring to.

        Phase 1 strategy: return the most recent task (by updated_at).
        Phase 2+ will add label / entity / semantic matching.
        """
        if hint:
            tasks = await self.list_tasks(conversation_id)
            for t in tasks:
                if hint in t.goal or (t.goal_summary and hint in t.goal_summary):
                    return t
        return await self.get_most_recent(conversation_id)
