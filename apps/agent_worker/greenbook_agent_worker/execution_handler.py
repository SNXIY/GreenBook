"""Compatibility import for the standalone Execution Queue worker."""

from greenbook_agent_api.services.queue_execution_handler import (
    RuntimeExecutionQueueHandler,
)

__all__ = ["RuntimeExecutionQueueHandler"]
