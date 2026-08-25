"""Durable, user-facing projection of one terminal Runtime execution."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa
from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ExecutionResultProjection(BaseModel):
    """Restart-safe result facts derived from canonical Execution state."""

    execution_id: str
    task_id: str = ""
    conversation_id: str
    run_id: str = ""
    trace_id: str = ""
    # Immutable Objective correlation carried by the queue envelope.
    objective_id: str = ""
    status: str = ""
    # Task-level completion (COMPLETED / IN_PROGRESS / FAILED / ...) derived
    # from Goal satisfaction, not from this single Execution's terminal state.
    task_status: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    schedule: dict[str, Any] | None = None
    next_actions: list[str] = Field(default_factory=list)
    summary: str = ""
    assistant_response: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=_now_iso)
    updated_at: str = Field(default_factory=_now_iso)


class ExecutionResultProjectionStore(Protocol):
    def save(self, projection: ExecutionResultProjection) -> ExecutionResultProjection: ...
    def get(self, execution_id: str) -> ExecutionResultProjection | None: ...

    def get_by_run_id(self, run_id: str) -> ExecutionResultProjection | None: ...

    def list_by_run_id(self, run_id: str) -> list[ExecutionResultProjection]: ...


class MemoryExecutionResultProjectionStore:
    """Process-local implementation used only by memory/test profiles."""

    def __init__(self) -> None:
        self._items: dict[str, ExecutionResultProjection] = {}

    def save(self, projection: ExecutionResultProjection) -> ExecutionResultProjection:
        current = self._items.get(projection.execution_id)
        saved = projection.model_copy(deep=True)
        if current is not None:
            saved.created_at = current.created_at
        saved.updated_at = _now_iso()
        self._items[saved.execution_id] = saved
        return saved.model_copy(deep=True)

    def get(self, execution_id: str) -> ExecutionResultProjection | None:
        projection = self._items.get(execution_id)
        return projection.model_copy(deep=True) if projection is not None else None

    def get_by_run_id(self, run_id: str) -> ExecutionResultProjection | None:
        for projection in self._items.values():
            if projection.run_id == run_id:
                return projection.model_copy(deep=True)
        return None

    def list_by_run_id(self, run_id: str) -> list[ExecutionResultProjection]:
        return [
            projection.model_copy(deep=True)
            for projection in self._items.values()
            if projection.run_id == run_id
        ]


result_projection_metadata = sa.MetaData()

execution_result_projections = sa.Table(
    "assistant_execution_result_projections",
    result_projection_metadata,
    sa.Column("execution_id", sa.String(128), primary_key=True),
    sa.Column("task_id", sa.String(128), nullable=False, default=""),
    sa.Column("conversation_id", sa.String(128), nullable=False),
    sa.Column("run_id", sa.String(128), nullable=False, default=""),
    sa.Column("trace_id", sa.String(128), nullable=False, default=""),
    sa.Column("objective_id", sa.String(128), nullable=False, default=""),
    sa.Column("status", sa.String(32), nullable=False, default=""),
    sa.Column("task_status", sa.String(32), nullable=False, default=""),
    sa.Column("artifacts", sa.JSON, nullable=False, default=list),
    sa.Column("schedule", sa.JSON),
    sa.Column("next_actions", sa.JSON, nullable=False, default=list),
    sa.Column("summary", sa.Text, nullable=False, default=""),
    sa.Column("assistant_response", sa.JSON, nullable=False, default=dict),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("updated_at", sa.String(64), nullable=False),
)


class PostgresExecutionResultProjectionStore:
    """SQLAlchemy store shared by API and Worker Runtime composition roots."""

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            result_projection_metadata.create_all(bind)
            if getattr(bind, "dialect", None) is not None and bind.dialect.name == "postgresql":
                with bind.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE assistant_execution_result_projections "
                        "ADD COLUMN IF NOT EXISTS task_status VARCHAR(32) NOT NULL DEFAULT ''"
                    )
                    connection.exec_driver_sql(
                        "ALTER TABLE assistant_execution_result_projections "
                        "ADD COLUMN IF NOT EXISTS objective_id VARCHAR(128) NOT NULL DEFAULT ''"
                    )

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def save(self, projection: ExecutionResultProjection) -> ExecutionResultProjection:
        current = self.get(projection.execution_id)
        saved = projection.model_copy(deep=True)
        if current is not None:
            saved.created_at = current.created_at
        saved.updated_at = _now_iso()
        values = saved.model_dump(mode="json")
        with self._connect() as conn:
            update_values = {
                key: value
                for key, value in values.items()
                if key not in {"execution_id", "created_at"}
            }
            if conn.dialect.name == "postgresql":
                from sqlalchemy.dialects.postgresql import insert

                conn.execute(
                    insert(execution_result_projections)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[execution_result_projections.c.execution_id],
                        set_=update_values,
                    )
                )
            elif conn.dialect.name == "sqlite":
                from sqlalchemy.dialects.sqlite import insert

                conn.execute(
                    insert(execution_result_projections)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[execution_result_projections.c.execution_id],
                        set_=update_values,
                    )
                )
            elif current is None:
                conn.execute(sa.insert(execution_result_projections).values(**values))
            else:
                conn.execute(
                    sa.update(execution_result_projections)
                    .where(
                        execution_result_projections.c.execution_id
                        == saved.execution_id
                    )
                    .values(**update_values)
                )
        return saved.model_copy(deep=True)

    def get(self, execution_id: str) -> ExecutionResultProjection | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(execution_result_projections).where(
                    execution_result_projections.c.execution_id == execution_id
                )
            ).mappings().first()
        return ExecutionResultProjection.model_validate(dict(row)) if row else None

    def get_by_run_id(self, run_id: str) -> ExecutionResultProjection | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(execution_result_projections).where(
                    execution_result_projections.c.run_id == run_id
                )
            ).mappings().first()
        return ExecutionResultProjection.model_validate(dict(row)) if row else None

    def list_by_run_id(self, run_id: str) -> list[ExecutionResultProjection]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(execution_result_projections)
                .where(execution_result_projections.c.run_id == run_id)
                .order_by(
                    execution_result_projections.c.created_at,
                    execution_result_projections.c.execution_id,
                )
            ).mappings().all()
        return [ExecutionResultProjection.model_validate(dict(row)) for row in rows]


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args: Any) -> None:
        return None


__all__ = [
    "ExecutionResultProjection",
    "ExecutionResultProjectionStore",
    "MemoryExecutionResultProjectionStore",
    "PostgresExecutionResultProjectionStore",
    "execution_result_projections",
    "result_projection_metadata",
]
