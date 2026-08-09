"""Unified public identifier reference for legacy and Runtime executions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExecutionReference(BaseModel):
    """Identifiers only; execution state remains outside this contract."""

    model_config = ConfigDict(frozen=True)

    run_id: str | None = None
    execution_id: str | None = None
    task_id: str | None = None
    source: str = "UNMAPPED"


def build_execution_reference(
    *,
    run_id: str | None = None,
    execution_id: str | None = None,
    task_id: str | None = None,
) -> ExecutionReference:
    """Build a reference from already-resolved IDs without reading Runtime state."""

    if execution_id:
        source = "RUNTIME" if run_id else "EXECUTION_ONLY"
    elif run_id:
        source = "LEGACY_ONLY"
    else:
        source = "UNMAPPED"
    return ExecutionReference(
        run_id=run_id,
        execution_id=execution_id,
        task_id=task_id,
        source=source,
    )


__all__ = ["ExecutionReference", "build_execution_reference"]
