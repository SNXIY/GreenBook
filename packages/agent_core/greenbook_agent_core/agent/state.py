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
    # Goal-level completion (goal_id), restored from durable resume state.
    # Kept separate from completed_task_ids (task/step namespace): a Goal is
    # satisfied by durable business facts, and every TaskNode owned by a
    # satisfied Goal is skipped by next-task selection.
    completed_goal_ids: list[str] = Field(default_factory=list)
    # Task nodes that have been durably submitted to the Runtime (QUEUED /
    # SUBMITTED) but are not yet complete.  Submission is a hand-off boundary,
    # never a completion: activity/next-task selection must not present
    # submitted work as finished, and the loop must not submit it again while
    # it is in flight.
    submitted_task_ids: list[str] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    execution_results: list[dict[str, Any]] = Field(default_factory=list)
    planning_decisions: list[dict[str, Any]] = Field(default_factory=list)
    preferred_tool_name: str = ""
    preferred_tool_arguments: dict[str, Any] = Field(default_factory=dict)
    # Control-plane convergence markers.  Plan/revision counters are not
    # progress by themselves; these fields track the last business-state
    # fingerprint seen by the generic no-progress guard.
    no_progress_fingerprint: str = ""
    no_progress_count: int = 0
    root_error_code: str = ""
    root_error_message: str = ""
    root_error_goal_id: str = ""
    root_error_iteration: int = 0
    # Deterministic tool/capability rejections (TOOL_CAPABILITY_MISMATCH,
    # TOOL_NOT_IN_CATALOG, ...).  A model may be corrected ONCE; repeating the
    # same deterministic rejection is a hard path failure, not a retryable one.
    deterministic_rejections: int = 0
    # First-meaningful-feedback timing markers (ISO timestamps) recorded by
    # the loop for the current run; consumed by the API layer for TTFA etc.
    timings: dict[str, Any] = Field(default_factory=dict)


__all__ = ["AgentState", "AgentStatus", "Observation"]
