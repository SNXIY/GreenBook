"""Compatibility import for the RuntimeContext core contract."""

from greenbook_agent_core.execution.runtime_context import (
    RuntimeContext,
    TargetContext,
    TaskContext,
)

__all__ = ["RuntimeContext", "TargetContext", "TaskContext"]
