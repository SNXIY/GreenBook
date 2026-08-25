"""Compatibility import for the shared RuntimeAgentService."""

from greenbook_agent_core.execution.runtime_agent_service import (
    RuntimeAgentService,
    RuntimeCompletionCallback,
)

__all__ = ["RuntimeAgentService", "RuntimeCompletionCallback"]
