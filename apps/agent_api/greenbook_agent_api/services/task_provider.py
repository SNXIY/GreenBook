"""Compatibility import for the shared Task persistence boundary."""

from greenbook_agent_core.task.provider import (
    TaskProvider,
    TaskProviderError,
    TaskProviderRepository,
    TaskScope,
)

__all__ = [
    "TaskProvider",
    "TaskProviderError",
    "TaskProviderRepository",
    "TaskScope",
]
