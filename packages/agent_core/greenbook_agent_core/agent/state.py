"""State contracts for the Goal-driven Agent Intelligence loop."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from greenbook_contracts.tool_contract import ToolMetadata
from pydantic import BaseModel, ConfigDict, Field

from greenbook_agent_core.command.models import Command
from greenbook_agent_core.goal.models import Goal, GoalTree, TaskNode


class AgentStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    WAITING_HUMAN = "WAITING_HUMAN"
    FAILED = "FAILED"
    MAX_ITERATIONS = "MAX_ITERATIONS"


class Observation(BaseModel):
    """One Observe result supplied to Reason and Reflection."""

    model_config = ConfigDict(extra="allow")

    goal: dict[str, Any] = Field(default_factory=dict)
    current_task: dict[str, Any] = Field(default_factory=dict)
    current_task_status: str = ""
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    task: Any | None = None
    plan_version: int = 0
    planning_decisions: list[dict[str, Any]] = Field(default_factory=list)
    last_result: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    context_snapshot_id: str = ""
    memory_ids_used: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    execution_states: list[dict[str, Any]] = Field(default_factory=list)
    waiting_human: dict[str, Any] = Field(default_factory=dict)
    resume_context: dict[str, Any] = Field(default_factory=dict)
    result_status: str = ""
    resource_count: int = Field(default=0, ge=0)
    missing_required_reference: str = ""
    available_fallback_capabilities: list[str] = Field(default_factory=list)
    failure_kind: str = ""


class AgentState(BaseModel):
    """Durable-in-memory state for one AgentLoop run.

    The state is intentionally separate from PlanExecution.  It records
    reasoning facts and references; execution truth remains in the existing
    Execution/Worker/Queue layer.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="allow")

    goal: Goal | None = None
    current_task: TaskNode | None = None
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    available_tools: list[ToolMetadata] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    history: list[dict[str, Any]] = Field(default_factory=list)
    memory_snapshot: dict[str, Any] = Field(default_factory=dict)
    context_snapshot: dict[str, Any] = Field(default_factory=dict)
    context_snapshot_id: str = ""
    memory_ids_used: list[str] = Field(default_factory=list)
    resume_context: dict[str, Any] = Field(default_factory=dict)
    iteration: int = 0

    command: Command | None = None
    goal_tree: GoalTree | None = None
    status: AgentStatus = AgentStatus.RUNNING
    finished: bool = False
    last_error: str = ""
    completed_task_ids: list[str] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    planning_decisions: list[dict[str, Any]] = Field(default_factory=list)
    preferred_tool_name: str = ""
    preferred_tool_arguments: dict[str, Any] = Field(default_factory=dict)


__all__ = ["AgentState", "AgentStatus", "Observation"]
