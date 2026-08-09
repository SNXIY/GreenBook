"""Repository layer for Assistant persistence — SQLAlchemy Core + asyncpg.

Each repository encapsulates SQL for a single table.  Routes call repositories;
raw SQL never appears in the HTTP layer.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import AsyncSession

# ── table metadata ──────────────────────────────────────────────────

metadata = sa.MetaData()

_conversations = sa.Table(
    "assistant_conversations",
    metadata,
    sa.Column("conversation_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("title", sa.String(120)),
    sa.Column("active_task_id", sa.String(128)),
    sa.Column("active_artifact_id", sa.String(128)),
    sa.Column("active_draft_id", sa.String(128)),
    sa.Column("active_schedule_id", sa.String(128)),
    sa.Column("active_post_id", sa.String(128)),
    sa.Column("recent_entities", JSONB, default=list),
    sa.Column("recent_tool_calls", JSONB, default=list),
    sa.Column("pending_approval", JSONB),
    sa.Column("last_successful_run_id", sa.String(128)),
    sa.Column("conversation_summary", sa.Text),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
    sa.Column("updated_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)

_messages = sa.Table(
    "assistant_messages",
    metadata,
    sa.Column("message_id", UUID(as_uuid=True), primary_key=True, default=uuid.uuid4),
    sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("assistant_conversations.conversation_id"), nullable=False),
    sa.Column("role", sa.String(16), nullable=False),
    sa.Column("content", sa.Text, nullable=False),
    sa.Column("trace_id", sa.String(64)),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)

_runs = sa.Table(
    "assistant_runs",
    metadata,
    sa.Column("run_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("assistant_conversations.conversation_id"), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    # Legacy history only. Runtime status belongs to PlanExecution.
    sa.Column("status", sa.String(32), nullable=True),
    sa.Column("content", sa.Text),
    sa.Column("error_code", sa.String(64)),
    sa.Column("error_message", sa.Text),
    sa.Column("tool_rounds", sa.Integer, default=0),
    sa.Column("trace_id", sa.String(64)),
    sa.Column("events", JSONB, default=list),
    sa.Column("approval_id", sa.String(128)),
    sa.Column("session_snapshot", JSONB),
    sa.Column("partial_results", JSONB),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)

RUN_PROJECTION_ALLOWED_FIELDS = frozenset({
    "run_id", "conversation_id", "user_id", "tenant_id", "content", "trace_id",
})
RUN_PROJECTION_FORBIDDEN_FIELDS = frozenset({
    "status", "events", "error_code", "error_message", "tool_rounds",
    "partial_results", "progress", "current_step", "execution_state",
    "checkpoint", "retry_state",
})


class RunProjectionContractError(ValueError):
    """Raised when a run write crosses the Runtime/Legacy projection boundary."""


def validate_run_projection_fields(
    fields: dict[str, Any], *, legacy_projection: bool,
) -> None:
    if not legacy_projection:
        forbidden = sorted(RUN_PROJECTION_FORBIDDEN_FIELDS.intersection(fields))
        if forbidden:
            raise RunProjectionContractError(
                "Runtime-backed assistant_runs writes cannot contain: "
                + ", ".join(forbidden)
            )
        unexpected = sorted(set(fields) - RUN_PROJECTION_ALLOWED_FIELDS)
        if unexpected:
            raise RunProjectionContractError(
                "Runtime-backed assistant_runs writes contain unsupported fields: "
                + ", ".join(unexpected)
            )

_approvals = sa.Table(
    "assistant_approvals",
    metadata,
    sa.Column("approval_id", UUID(as_uuid=True), primary_key=True),
    sa.Column("conversation_id", UUID(as_uuid=True), sa.ForeignKey("assistant_conversations.conversation_id"), nullable=False),
    sa.Column("run_id", UUID(as_uuid=True), nullable=True),
    sa.Column("execution_id", sa.String(128), nullable=True),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("tenant_id", sa.String(64), nullable=False),
    sa.Column("operation", sa.String(128), nullable=False),
    sa.Column("resource_id", sa.String(128)),
    sa.Column("description", sa.Text),
    sa.Column("payload", JSONB),
    sa.Column("status", sa.String(16), nullable=False, default="PENDING"),
    sa.Column("version", sa.Integer, default=1),
    sa.Column("created_at", sa.DateTime(timezone=True), default=lambda: datetime.now(UTC)),
)


async def _ensure_tables(session: AsyncSession) -> None:
    """Create tables if they don't exist (idempotent)."""
    # create_all needs a Connection, not a Session — use the bound engine.
    async with session.bind.begin() as conn:
        await conn.run_sync(metadata.create_all, checkfirst=True)


# ── helpers ─────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _json_dump(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row: Any, *, time_cols: tuple[str, ...] = ()) -> dict[str, Any]:
    """Convert a Row to a plain dict, serialising UUID/datetime to strings."""
    if row is None:
        return {}
    d = dict(row._mapping)
    for col in time_cols:
        val = d.get(col)
        if isinstance(val, datetime):
            d[col] = val.isoformat()
    # UUID columns → string (Pydantic expects str, not uuid.UUID)
    for key, val in d.items():
        if isinstance(val, uuid.UUID):
            d[key] = str(val)
    return d


# ── ConversationRepository ───────────────────────────────────────────

class ConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        title: str | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        now = _now_utc()
        values: dict[str, Any] = {
            "conversation_id": uuid.UUID(conversation_id),
            "user_id": user_id,
            "tenant_id": tenant_id,
            "title": title,
            "active_task_id": None,
            "active_artifact_id": None,
            "active_draft_id": None,
            "active_schedule_id": None,
            "active_post_id": None,
            "recent_entities": [],
            "recent_tool_calls": [],
            "pending_approval": None,
            "last_successful_run_id": None,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        await self._session.execute(
            sa.insert(_conversations).values(**values)
        )
        await self._session.commit()
        values["created_at"] = now.isoformat()
        values["updated_at"] = now.isoformat()
        values["conversation_id"] = conversation_id
        return values

    async def find_by_id(self, conversation_id: str) -> dict[str, Any] | None:
        row = await self._session.execute(
            sa.select(_conversations).where(
                _conversations.c.conversation_id == uuid.UUID(conversation_id)
            )
        )
        result = row.first()
        if result is None:
            return None
        return _row_to_dict(result, time_cols=("created_at", "updated_at"))

    async def find_all_by_user(self, user_id: str, tenant_id: str) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            sa.select(_conversations)
            .where(
                sa.and_(
                    _conversations.c.user_id == user_id,
                    _conversations.c.tenant_id == tenant_id,
                )
            )
            .order_by(_conversations.c.updated_at.desc())
        )
        return [_row_to_dict(r, time_cols=("created_at", "updated_at")) for r in rows.all()]

    async def update(
        self,
        conversation_id: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        existing = await self.find_by_id(conversation_id)
        if existing is None:
            return None
        version = int(existing.get("version", 1))
        values = {**fields, "updated_at": _now_utc(), "version": version + 1}
        await self._session.execute(
            sa.update(_conversations)
            .where(
                sa.and_(
                    _conversations.c.conversation_id == uuid.UUID(conversation_id),
                    _conversations.c.version == version,
                )
            )
            .values(**values)
        )
        await self._session.commit()
        return await self.find_by_id(conversation_id)

    async def delete(self, conversation_id: str) -> None:
        await self._session.execute(
            sa.delete(_conversations).where(
                _conversations.c.conversation_id == uuid.UUID(conversation_id)
            )
        )
        await self._session.commit()


# ── MessageRepository ────────────────────────────────────────────────

class MessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(
        self,
        conversation_id: str,
        role: str,
        content: str,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {
            "conversation_id": uuid.UUID(conversation_id),
            "role": role,
            "content": content,
            "trace_id": trace_id,
            "version": 1,
            "created_at": _now_utc(),
        }
        await self._session.execute(
            sa.insert(_messages).values(**values)
        )
        await self._session.commit()
        return {"role": role, "content": content, "trace_id": trace_id, "created_at": _now_iso()}

    async def find_by_conversation(
        self,
        conversation_id: str,
        *,
        roles: tuple[str, ...] | None = None,
    ) -> list[dict[str, Any]]:
        stmt = sa.select(_messages).where(
            _messages.c.conversation_id == uuid.UUID(conversation_id)
        )
        if roles:
            stmt = stmt.where(_messages.c.role.in_(roles))
        stmt = stmt.order_by(_messages.c.created_at.asc())
        rows = await self._session.execute(stmt)
        return [
            {
                "role": r.role,
                "content": r.content,
                "trace_id": r.trace_id,
                "created_at": r.created_at.isoformat() if r.created_at else "",
            }
            for r in rows.all()
        ]


# ── LegacyRunHistoryRepository ──────────────────────────────────────

class LegacyRunHistoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, **fields: Any) -> dict[str, Any]:
        values = dict(fields)
        if "_legacy_projection" not in values:
            raise RunProjectionContractError(
                "LegacyRunHistoryRepository.create() requires explicit _legacy_projection"
            )
        legacy_projection = bool(values.pop("_legacy_projection"))
        validate_run_projection_fields(values, legacy_projection=legacy_projection)
        values.setdefault("version", 1)
        if legacy_projection:
            values.setdefault("events", [])
            values.setdefault("tool_rounds", 0)
        if "run_id" in values and values["run_id"] and isinstance(values["run_id"], str):
            values["run_id"] = uuid.UUID(values["run_id"])
        if "conversation_id" in values and isinstance(values["conversation_id"], str):
            values["conversation_id"] = uuid.UUID(values["conversation_id"])
        values.setdefault("created_at", _now_utc())
        if legacy_projection:
            values.setdefault("status", "IN_PROGRESS")
        await self._session.execute(sa.insert(_runs).values(**values))
        await self._session.commit()
        return {**values, "run_id": str(values.get("run_id", ""))}

    async def find_by_id(self, run_id: str) -> dict[str, Any] | None:
        row = await self._session.execute(
            sa.select(_runs).where(_runs.c.run_id == uuid.UUID(run_id))
        )
        result = row.first()
        if result is None:
            return None
        return _row_to_dict(result)

    async def find_all_by_user(self, user_id: str, tenant_id: str, limit: int = 30) -> list[dict[str, Any]]:
        rows = await self._session.execute(
            sa.select(_runs)
            .where(
                sa.and_(
                    _runs.c.user_id == user_id,
                    _runs.c.tenant_id == tenant_id,
                )
            )
            .order_by(_runs.c.created_at.desc())
            .limit(limit)
        )
        return [_row_to_dict(r) for r in rows.all()]

    async def update(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        if "_legacy_projection" not in fields:
            raise RunProjectionContractError(
                "LegacyRunHistoryRepository.update() requires explicit _legacy_projection"
            )
        legacy_projection = bool(fields.pop("_legacy_projection"))
        validate_run_projection_fields(
            {"run_id": run_id, **fields}, legacy_projection=legacy_projection
        )
        existing = await self.find_by_id(run_id)
        if existing is None:
            return None
        version = int(existing.get("version", 1))
        values = {**fields, "version": version + 1}
        await self._session.execute(
            sa.update(_runs)
            .where(
                sa.and_(
                    _runs.c.run_id == uuid.UUID(run_id),
                    _runs.c.version == version,
                )
            )
            .values(**values)
        )
        await self._session.commit()
        return await self.find_by_id(run_id)


# Compatibility alias for existing API and external callers. The canonical
# name makes the assistant_runs history/projection responsibility explicit.
RunRepository = LegacyRunHistoryRepository


# ── ApprovalRepository ───────────────────────────────────────────────

class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, **fields: Any) -> dict[str, Any]:
        values = dict(fields)
        values.setdefault("version", 1)
        values.setdefault("status", "PENDING")
        if "approval_id" in values and isinstance(values["approval_id"], str):
            values["approval_id"] = uuid.UUID(values["approval_id"])
        if "conversation_id" in values and isinstance(values["conversation_id"], str):
            values["conversation_id"] = uuid.UUID(values["conversation_id"])
        if "run_id" in values and isinstance(values["run_id"], str):
            values["run_id"] = uuid.UUID(values["run_id"])
        values.setdefault("created_at", _now_utc())
        await self._session.execute(sa.insert(_approvals).values(**values))
        await self._session.commit()
        return {**values, "approval_id": str(values.get("approval_id", ""))}

    async def find_by_id(self, approval_id: str) -> dict[str, Any] | None:
        row = await self._session.execute(
            sa.select(_approvals).where(_approvals.c.approval_id == uuid.UUID(approval_id))
        )
        result = row.first()
        if result is None:
            return None
        return _row_to_dict(result)

    async def find_by_run_id(self, run_id: str) -> dict[str, Any] | None:
        row = await self._session.execute(
            sa.select(_approvals)
            .where(_approvals.c.run_id == uuid.UUID(run_id))
            .order_by(_approvals.c.created_at.desc())
        )
        result = row.first()
        return _row_to_dict(result) if result is not None else None

    async def find_by_execution_id(self, execution_id: str) -> dict[str, Any] | None:
        row = await self._session.execute(
            sa.select(_approvals)
            .where(_approvals.c.execution_id == execution_id)
            .order_by(_approvals.c.created_at.desc())
        )
        result = row.first()
        return _row_to_dict(result) if result is not None else None

    async def update(self, approval_id: str, **fields: Any) -> dict[str, Any] | None:
        existing = await self.find_by_id(approval_id)
        if existing is None:
            return None
        version = int(existing.get("version", 1))
        values = {**fields, "version": version + 1}
        await self._session.execute(
            sa.update(_approvals)
            .where(
                sa.and_(
                    _approvals.c.approval_id == uuid.UUID(approval_id),
                    _approvals.c.version == version,
                )
            )
            .values(**values)
        )
        await self._session.commit()
        return await self.find_by_id(approval_id)
