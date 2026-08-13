"""ID-only compatibility mapping between legacy runs and PlanExecution.

This adapter deliberately does not model or own execution state.  The
``execution_id`` in a bound link must come from an existing ``PlanExecution``
created by the Execution Runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field

from .execution_reference import ExecutionReference, build_execution_reference
from .run_execution_repository import (
    InMemoryRunExecutionLinkRepository,
    RunExecutionLinkRepository,
)


class RunExecutionLinkSource(StrEnum):
    """How a run/execution relationship was established."""

    CREATED = "CREATED"
    BACKFILLED = "BACKFILLED"
    LEGACY_ONLY = "LEGACY_ONLY"


class RunExecutionLink(BaseModel):
    """An identifier relationship, not an execution state object."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    execution_id: str | None = None
    conversation_id: str = ""
    task_id: str = ""
    mapping_source: RunExecutionLinkSource = RunExecutionLinkSource.CREATED
    mapping_version: str = "1"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class DuplicateRunExecutionBindingError(ValueError):
    """Raised when an ID is already bound to a different counterpart."""


class RunExecutionAdapter:
    """Resolve legacy ``run_id`` values to canonical execution IDs.

    The adapter delegates storage to a repository. The default repository is
    in-memory for compatibility/test environments; production wiring should
    inject ``SqlAlchemyRunExecutionLinkRepository``.
    """

    def __init__(self, repository: RunExecutionLinkRepository | None = None) -> None:
        self._repository = repository or InMemoryRunExecutionLinkRepository()
        self._lock = RLock()

    def bind_run_execution(
        self,
        run_id: str,
        execution_id: str,
        *,
        conversation_id: str = "",
        task_id: str = "",
        mapping_source: RunExecutionLinkSource = RunExecutionLinkSource.CREATED,
        mapping_version: str = "1",
    ) -> RunExecutionLink:
        """Bind an existing PlanExecution ID to a legacy run ID.

        Repeating the exact same bind is idempotent.  Reusing either ID for a
        different counterpart is rejected to prevent ambiguous lookups.
        """

        if not run_id or not execution_id:
            raise ValueError("run_id and execution_id are required")
        with self._lock:
            existing = self._repository.find_by_run_id(run_id)
            if existing is not None:
                if existing.execution_id != execution_id:
                    raise DuplicateRunExecutionBindingError(
                        f"run_id {run_id!r} is already bound to another execution"
                    )
                return existing

            existing_execution = self._repository.find_by_execution_id(execution_id)
            if existing_execution is not None and existing_execution.run_id != run_id:
                raise DuplicateRunExecutionBindingError(
                    f"execution_id {execution_id!r} is already bound to another run"
                )

            link = RunExecutionLink(
                run_id=run_id,
                execution_id=execution_id,
                conversation_id=conversation_id,
                task_id=task_id,
                mapping_source=mapping_source,
                mapping_version=mapping_version,
            )
            try:
                return self._repository.save_link(link)
            except ValueError as exc:
                raise DuplicateRunExecutionBindingError(str(exc)) from exc

    def register_legacy_only(
        self,
        run_id: str,
        *,
        conversation_id: str = "",
        task_id: str = "",
        mapping_version: str = "1",
    ) -> RunExecutionLink:
        """Register a legacy run that has no corresponding PlanExecution."""

        if not run_id:
            raise ValueError("run_id is required")
        with self._lock:
            existing = self._repository.find_by_run_id(run_id)
            if existing is not None:
                return existing
            link = RunExecutionLink(
                run_id=run_id,
                conversation_id=conversation_id,
                task_id=task_id,
                mapping_source=RunExecutionLinkSource.LEGACY_ONLY,
                mapping_version=mapping_version,
            )
            try:
                return self._repository.save_link(link)
            except ValueError as exc:
                raise DuplicateRunExecutionBindingError(str(exc)) from exc

    def resolve_link_by_run(self, run_id: str) -> RunExecutionLink | None:
        with self._lock:
            return self._repository.find_by_run_id(run_id)

    def resolve_link_by_execution(self, execution_id: str) -> RunExecutionLink | None:
        with self._lock:
            return self._repository.find_by_execution_id(execution_id)

    def resolve_execution(self, run_id: str) -> str | None:
        """Return the canonical execution ID, or ``None`` for legacy-only."""

        link = self.resolve_link_by_run(run_id)
        return link.execution_id if link is not None else None

    def resolve_run(self, execution_id: str) -> str | None:
        """Return the legacy run ID associated with an execution."""

        link = self.resolve_link_by_execution(execution_id)
        return link.run_id if link is not None else None

    def to_execution_reference(
        self,
        *,
        run_id: str | None = None,
        execution_id: str | None = None,
        task_id: str | None = None,
    ) -> ExecutionReference:
        """Resolve a public ID reference without accessing execution state."""

        link = None
        if run_id:
            link = self.resolve_link_by_run(run_id)
        elif execution_id:
            link = self.resolve_link_by_execution(execution_id)

        resolved_run_id = run_id or (link.run_id if link is not None else None)
        resolved_execution_id = execution_id or (
            link.execution_id if link is not None else None
        )
        resolved_task_id = task_id or (
            (link.task_id or None) if link is not None else None
        )
        return build_execution_reference(
            run_id=resolved_run_id,
            execution_id=resolved_execution_id,
            task_id=resolved_task_id,
        )


__all__ = [
    "DuplicateRunExecutionBindingError",
    "RunExecutionAdapter",
    "RunExecutionLink",
    "RunExecutionLinkSource",
]
