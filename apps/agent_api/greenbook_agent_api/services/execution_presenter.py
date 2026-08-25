"""Compatibility imports for the shared Runtime presentation contract."""

from greenbook_agent_core.execution.presenter import (
    AgentResponse,
    BusinessEntityProjection,
    BusinessProjection,
    ExecutionPresenter,
    ExecutionResultPresenter,
    PresentationArtifact,
    business_state_for_resource,
    present_execution_result,
    project_business_result,
)

__all__ = [
    "AgentResponse",
    "BusinessEntityProjection",
    "BusinessProjection",
    "ExecutionPresenter",
    "ExecutionResultPresenter",
    "PresentationArtifact",
    "business_state_for_resource",
    "present_execution_result",
    "project_business_result",
]
