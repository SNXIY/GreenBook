"""Serializable correlation context shared by Runtime observability layers."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TraceContext(BaseModel):
    """Correlation scope for one request and its derived Runtime operations."""

    model_config = ConfigDict(frozen=True)

    conversation_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    task_id: str = ""
    execution_id: str = ""
    step_id: str = ""
    invocation_id: str = ""
    tool_call_id: str = ""
    operation_id: str = ""

    def with_updates(self, **values: str | None) -> TraceContext:
        """Return a copy with only non-``None`` values updated."""

        return self.model_copy(
            update={key: value for key, value in values.items() if value is not None}
        )

    def for_execution(self, execution_id: str) -> TraceContext:
        return self.with_updates(execution_id=execution_id)

    def for_step(self, step_id: str) -> TraceContext:
        return self.with_updates(step_id=step_id)

    def for_invocation(
        self,
        invocation_id: str,
        *,
        tool_call_id: str | None = None,
        operation_id: str | None = None,
    ) -> TraceContext:
        return self.with_updates(
            invocation_id=invocation_id,
            tool_call_id=tool_call_id,
            operation_id=operation_id,
        )

    def for_operation(self, operation_id: str) -> TraceContext:
        return self.with_updates(operation_id=operation_id)


__all__ = ["TraceContext"]
