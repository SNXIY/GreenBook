"""PostgreSQL storage for canonical Tasks and their projections."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

from greenbook_agent_core.planning.contracts import PlanRevision

from .models import (
    ArtifactRef,
    Task,
    TaskExecutionRef,
    TaskGoal,
    TaskResourceRef,
    TaskRevision,
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
    sa.Column("priority", sa.Integer, default=0),
    sa.Column("task_type", sa.String(64), default="GOAL_DRIVEN"),
    sa.Column("execution_mode", sa.String(32), default="AUTO"),
    sa.Column("root_goal_id", sa.String(128)),
    sa.Column("goal_tree_version", sa.Integer, default=0),
    sa.Column("goal_tree_snapshot", JSONB, default=dict),
    sa.Column("plan_version", sa.Integer, default=0),
    sa.Column("plan_history", JSONB, default=list),
    sa.Column("active_execution_id", sa.String(128)),
    sa.Column("artifacts", JSONB, default=list),
    sa.Column("depends_on", JSONB, default=list),
    sa.Column("goals", JSONB, default=list),
    sa.Column("revisions", JSONB, default=list),
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


class TaskRepository:
    """Low-level DB access for assistant_tasks / assistant_artifacts."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_tables(self) -> None:
        async with self._session.bind.begin() as conn:
            await conn.run_sync(_tasks.metadata.create_all, checkfirst=True)
            # Phase15-B adds a task read-model projection.  Keep this additive
            # and idempotent so existing Phase15-A databases remain usable.
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS goals JSONB"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS revisions JSONB"
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
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 0"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS task_type VARCHAR(64) DEFAULT 'GOAL_DRIVEN'"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(32) DEFAULT 'AUTO'"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS root_goal_id VARCHAR(128)"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS goal_tree_version INTEGER DEFAULT 0"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS goal_tree_snapshot JSONB"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS plan_version INTEGER DEFAULT 0"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS plan_history JSONB"
            )
            await conn.exec_driver_sql(
                "ALTER TABLE assistant_tasks ADD COLUMN IF NOT EXISTS active_execution_id VARCHAR(128)"
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
            "priority": task.priority,
            "task_type": task.task_type,
            "execution_mode": task.execution_mode,
            "root_goal_id": task.root_goal_id,
            "goal_tree_version": task.goal_tree_version,
            "goal_tree_snapshot": task.goal_tree_snapshot,
            "plan_version": task.plan_version,
            "plan_history": [r.model_dump(mode="json") for r in task.plan_history],
            "active_execution_id": task.active_execution_id,
            "artifacts": [a.model_dump(mode="json") for a in task.artifacts],
            "depends_on": task.depends_on,
            "goals": [g.model_dump(mode="json") for g in task.goals],
            "revisions": [r.model_dump(mode="json") for r in task.revisions],
            "execution_refs": [e.model_dump(mode="json") for e in task.execution_refs],
            "resource_index": [r.model_dump(mode="json") for r in task.resource_index],
            "last_action": task.last_action,
            "action_history": task.action_history,
            "last_error": task.last_error,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "version": task.version,
            "created_at": datetime.fromisoformat(task.created_at),
            "updated_at": datetime.fromisoformat(task.updated_at),
            "completed_at": (
                datetime.fromisoformat(task.completed_at)
                if task.completed_at else None
            ),
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
        if isinstance(values.get("completed_at"), str):
            values["completed_at"] = datetime.fromisoformat(values["completed_at"])
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
            priority=int(d.get("priority", 0) or 0),
            task_type=str(d.get("task_type", "GOAL_DRIVEN") or "GOAL_DRIVEN"),
            execution_mode=str(d.get("execution_mode", "AUTO") or "AUTO"),
            root_goal_id=d.get("root_goal_id"),
            goal_tree_version=int(d.get("goal_tree_version", 0) or 0),
            goal_tree_snapshot=dict(d.get("goal_tree_snapshot") or {}),
            plan_version=int(d.get("plan_version", 0) or 0),
            plan_history=[
                PlanRevision(**r) for r in (d.get("plan_history") or [])
            ],
            active_execution_id=d.get("active_execution_id"),
            artifacts=[
                ArtifactRef(**a) for a in (d.get("artifacts") or [])
            ],
            depends_on=list(d.get("depends_on") or []),
            goals=[TaskGoal(**g) for g in (d.get("goals") or [])],
            revisions=[TaskRevision(**r) for r in (d.get("revisions") or [])],
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

    async def insert_task(self, task: Task) -> Task:
        """Persist a fully constructed canonical Task.

        ``create_task`` remains for the older API projection.  New callers
        hand the complete lifecycle model to this method so TaskManager is
        not forced through a user-request-shaped persistence API.
        """

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
