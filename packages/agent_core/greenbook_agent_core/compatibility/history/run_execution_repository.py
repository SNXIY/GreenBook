"""Persistence boundary for legacy run to canonical execution links."""

from __future__ import annotations

from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError


class RunExecutionLinkRepository(Protocol):
    """Repository contract used by :class:`RunExecutionAdapter`."""

    def save_link(self, link: Any) -> Any:
        ...

    def find_by_run_id(self, run_id: str) -> Any | None:
        ...

    def find_by_execution_id(self, execution_id: str) -> Any | None:
        ...

    def exists(
        self,
        *,
        run_id: str | None = None,
        execution_id: str | None = None,
    ) -> bool:
        ...


class InMemoryRunExecutionLinkRepository:
    """Small repository implementation for tests and no-DB compatibility."""

    def __init__(self) -> None:
        self._by_run: dict[str, Any] = {}
        self._by_execution: dict[str, Any] = {}

    def save_link(self, link: Any) -> Any:
        existing = self._by_run.get(link.run_id)
        if existing is not None and existing != link:
            raise ValueError(f"run_id {link.run_id!r} already exists")
        if link.execution_id:
            existing = self._by_execution.get(link.execution_id)
            if existing is not None and existing != link:
                raise ValueError(f"execution_id {link.execution_id!r} already exists")
            self._by_execution[link.execution_id] = link
        self._by_run[link.run_id] = link
        return link

    def find_by_run_id(self, run_id: str) -> Any | None:
        return self._by_run.get(run_id)

    def find_by_execution_id(self, execution_id: str) -> Any | None:
        return self._by_execution.get(execution_id)

    def exists(
        self,
        *,
        run_id: str | None = None,
        execution_id: str | None = None,
    ) -> bool:
        if (run_id is None) == (execution_id is None):
            raise ValueError("provide exactly one of run_id or execution_id")
        return (
            run_id in self._by_run
            if run_id is not None
            else execution_id in self._by_execution
        )


compatibility_metadata = sa.MetaData()

run_execution_links = sa.Table(
    "run_execution_link",
    compatibility_metadata,
    sa.Column("run_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), unique=True, nullable=True),
    sa.Column("conversation_id", sa.String(128), nullable=False, default=""),
    sa.Column("task_id", sa.String(128), nullable=False, default=""),
    sa.Column("mapping_source", sa.String(32), nullable=False),
    sa.Column("mapping_version", sa.String(32), nullable=False),
    sa.Column("created_at", sa.String(64), nullable=False),
)


class SqlAlchemyRunExecutionLinkRepository:
    """SQLAlchemy-backed repository for PostgreSQL or SQLite.

    The table is independent from ``assistant_runs`` and contains no
    execution status, event, or checkpoint data.
    """

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            compatibility_metadata.create_all(bind)

    def save_link(self, link: Any) -> Any:
        values = link.model_dump(mode="json")
        values["mapping_source"] = link.mapping_source.value
        try:
            with self._bind.begin() as conn:
                conn.execute(sa.insert(run_execution_links).values(**values))
        except IntegrityError as exc:
            raise ValueError("run/execution link already exists") from exc
        return link

    def find_by_run_id(self, run_id: str) -> Any | None:
        with self._bind.begin() as conn:
            row = conn.execute(
                sa.select(run_execution_links).where(
                    run_execution_links.c.run_id == run_id
                )
            ).mappings().first()
        return _to_link(row)

    def find_by_execution_id(self, execution_id: str) -> Any | None:
        with self._bind.begin() as conn:
            row = conn.execute(
                sa.select(run_execution_links).where(
                    run_execution_links.c.execution_id == execution_id
                )
            ).mappings().first()
        return _to_link(row)

    def exists(
        self,
        *,
        run_id: str | None = None,
        execution_id: str | None = None,
    ) -> bool:
        if (run_id is None) == (execution_id is None):
            raise ValueError("provide exactly one of run_id or execution_id")
        column = (
            run_execution_links.c.run_id
            if run_id is not None
            else run_execution_links.c.execution_id
        )
        value = run_id if run_id is not None else execution_id
        with self._bind.begin() as conn:
            return conn.execute(
                sa.select(sa.literal(True)).where(column == value).limit(1)
            ).scalar() is True


def _to_link(row: Any) -> Any | None:
    if row is None:
        return None
    # Lazy import avoids coupling the repository module to adapter internals.
    from .run_execution_link import RunExecutionLink, RunExecutionLinkSource

    return RunExecutionLink(
        run_id=row["run_id"],
        execution_id=row["execution_id"],
        conversation_id=row["conversation_id"] or "",
        task_id=row["task_id"] or "",
        mapping_source=RunExecutionLinkSource(row["mapping_source"]),
        mapping_version=row["mapping_version"],
        created_at=row["created_at"],
    )


PostgresRunExecutionLinkRepository = SqlAlchemyRunExecutionLinkRepository

__all__ = [
    "InMemoryRunExecutionLinkRepository",
    "PostgresRunExecutionLinkRepository",
    "RunExecutionLinkRepository",
    "SqlAlchemyRunExecutionLinkRepository",
    "compatibility_metadata",
    "run_execution_links",
]
