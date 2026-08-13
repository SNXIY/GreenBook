"""Pure API-boundary helpers for Runtime execution links."""

from __future__ import annotations

from greenbook_agent_core.compatibility.history import RunExecutionAdapter

from ..models.runtime_context import RuntimeContext
from ..models.runtime_result import RuntimeResult


def bind_runtime_result(
    adapter: RunExecutionAdapter,
    *,
    run_id: str,
    result: RuntimeResult,
    ctx: RuntimeContext,
    conversation_id: str,
) -> str | None:
    """Persist only the Runtime ID relationship at the API boundary."""
    execution_id = result.execution_id or ctx.execution_id
    if result.execution_path != "runtime" or not execution_id:
        return None
    adapter.bind_run_execution(
        run_id,
        execution_id,
        conversation_id=conversation_id,
        task_id=ctx.task_id,
    )
    return execution_id


__all__ = ["bind_runtime_result"]
