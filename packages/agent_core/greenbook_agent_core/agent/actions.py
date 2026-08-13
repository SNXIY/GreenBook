"""Structured actions and reflection results emitted by AgentLoop."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata
from pydantic import BaseModel, ConfigDict, Field

from greenbook_agent_core.goal.models import GoalTree

from .state import AgentState, AgentStatus


class AgentActionType(StrEnum):
    TOOL_CALL = "TOOL_CALL"
    CREATE_TASK = "CREATE_TASK"
    UPDATE_PLAN = "UPDATE_PLAN"
    ASK_USER = "ASK_USER"
    FINISH = "FINISH"


class AgentAction(BaseModel):
    """One structured Reason output."""

    model_config = ConfigDict(extra="forbid")

    action: AgentActionType
    tool_name: str = ""
    tool_args: dict[str, Any] = Field(default_factory=dict)
    goal_tree: GoalTree | None = None
    plan_patch: dict[str, Any] = Field(default_factory=dict)
    question: str = ""
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class SelectedTool(BaseModel):
    """ToolSelector output after catalog validation."""

    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    metadata: ToolMetadata | None = None

    @property
    def name(self) -> str:
        """Convenience spelling for callers that treat selection as a name."""

        return self.tool_name


class Reflection(BaseModel):
    """Structured Reflect output."""

    model_config = ConfigDict(extra="forbid")

    finished: bool = False
    needs_next_step: bool = True
    retry: bool = False
    adjust_plan: bool = False
    reason: str = ""


class AgentRunResult(BaseModel):
    """Result envelope returned by AgentLoop."""

    success: bool = False
    status: AgentStatus = AgentStatus.FAILED
    content: str = ""
    question: str = ""
    error_code: str = ""
    error_message: str = ""
    iterations: int = 0
    actions: list[dict[str, Any]] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    compiled_plan: dict[str, Any] | None = None
    state: AgentState | None = None


# Short aliases keep the public contract ergonomic without duplicating enums.
ActionType = AgentActionType


__all__ = [
    "ActionType",
    "AgentAction",
    "AgentActionType",
    "AgentRunResult",
    "Reflection",
    "SelectedTool",
]
