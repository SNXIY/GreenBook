"""Agent-facing resume projection over the existing execution durability.

This module does not create a second checkpoint or execution store.  It turns
Task, PlanExecution, Checkpoint, Ledger, and Artifact projections into the
small state the AgentLoop needs after a process restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from greenbook_agent_core.execution.models import (
    StepStatus,
)


class RecoveryKind(StrEnum):
    RESUME_EXECUTION = "RESUME_EXECUTION"
    REPLAN_FROM_FAILURE = "REPLAN_FROM_FAILURE"
    WAIT_FOR_EXTERNAL = "WAIT_FOR_EXTERNAL"
    WAIT_FOR_HUMAN = "WAIT_FOR_HUMAN"
    RETRY_STEP = "RETRY_STEP"
    ABORT_TASK = "ABORT_TASK"


class AgentRecoveryDecision(BaseModel):
    action: RecoveryKind
    reason: str = ""
    execution_id: str = ""
    step_id: str = ""
    reuse_result: bool = False


class ResumeContext(BaseModel):
    """Bounded AgentLoop restart projection; no hidden reasoning is stored."""

    task_id: str = ""
    goal_tree_version: int = 0
    plan_version: int = 0
    iteration: int = 0
    last_action: str = ""
    last_observation_summary: str = ""
    waiting_state: str = ""
    memory_ids_used: list[str] = Field(default_factory=list)
    completed_goal_ids: list[str] = Field(default_factory=list)
    completed_step_ids: list[str] = Field(default_factory=list)
    failed_step_id: str = ""
    failed_error_code: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    execution_id: str = ""
    recovery_action: RecoveryKind = RecoveryKind.RESUME_EXECUTION
    recovery_reason: str = ""
    trace_context: dict[str, Any] = Field(default_factory=dict)


class IdempotentRecoveryGuard:
    """Prevent replay of a completed side-effect operation.

    The durable StepExecution status is checked first.  A shared
    ToolExecutionLedger is consulted when the process crashed after the tool
    returned but before the step projection was committed.
    """

    def __init__(self, ledger: Any | None = None) -> None:
        self._ledger = ledger

    def completed_result(self, step: Any) -> dict[str, Any] | None:
        if getattr(step, "status", None) == StepStatus.COMPLETED:
            artifact = getattr(step, "output_artifact", None)
            return {
                "status": StepStatus.COMPLETED.value,
                "step_id": str(getattr(step, "step_id", "")),
                "artifact": _dump(artifact),
                "replayed": True,
            }
        key = str(getattr(step, "idempotency_key", "") or "")
        if self._ledger is not None and key:
            entry = self._ledger.try_replay(key)
            if entry is not None:
                return {
                    "status": "COMPLETED",
                    "step_id": str(getattr(step, "step_id", "")),
                    "result": _dump(getattr(entry, "result", {})),
                    "replayed": True,
                }
        return None

    def should_execute(self, step: Any) -> bool:
        return self.completed_result(step) is None


class AgentRecoveryService:
    """Classify restart state and build a bounded Agent resume context."""

    def decide(
        self,
        *,
        task: Any | None = None,
        execution: Any | None = None,
        checkpoint: Any | None = None,
    ) -> AgentRecoveryDecision:
        task_status = _value(task, "status")
        execution_status = _value(execution, "status")
        execution_id = str(_value(execution, "execution_id") or "")
        if task_status in {"CANCELLED", "COMPLETED"} or execution_status == "CANCELLED":
            return AgentRecoveryDecision(
                action=RecoveryKind.ABORT_TASK,
                reason="Task or execution is terminal.",
                execution_id=execution_id,
            )
        waiting = str(
            _value(task, "waiting_state")
            or _value(execution, "control_reason")
            or _value(checkpoint, "snapshot", {}).get("waiting_state", "")
        ).upper()
        if task_status == "WAITING_HUMAN" or execution_status in {"WAITING_HUMAN", "WAITING_APPROVAL"} or waiting == "WAITING_HUMAN":
            return AgentRecoveryDecision(
                action=RecoveryKind.WAIT_FOR_HUMAN,
                reason="Human input or approval is required.",
                execution_id=execution_id,
            )
        if task_status == "WAITING_EXTERNAL" or waiting == "WAITING_EXTERNAL":
            return AgentRecoveryDecision(
                action=RecoveryKind.WAIT_FOR_EXTERNAL,
                reason="The external operation has not completed.",
                execution_id=execution_id,
            )
        failed = _failed_step(execution)
        if failed is not None:
            if str(_value(failed, "error_code")).upper() in {
                "TIMEOUT", "NETWORK_ERROR", "RATE_LIMIT", "TEMPORARY_UNAVAILABLE"
            } and int(_value(failed, "retry_count") or 0) < int(_value(failed, "max_retries") or 0):
                return AgentRecoveryDecision(
                    action=RecoveryKind.RETRY_STEP,
                    reason="The failed step is retryable within its durable budget.",
                    execution_id=execution_id,
                    step_id=str(_value(failed, "step_id") or ""),
                )
            return AgentRecoveryDecision(
                action=RecoveryKind.REPLAN_FROM_FAILURE,
                reason="The failed step requires a new plan decision.",
                execution_id=execution_id,
                step_id=str(_value(failed, "step_id") or ""),
            )
        if execution_status in {"RUNNING", "PAUSED", "PENDING"} or task_status in {
            "RUNNING", "PAUSED", "READY", "PLANNING", "CREATED"
        }:
            return AgentRecoveryDecision(
                action=RecoveryKind.RESUME_EXECUTION,
                reason="Resume from the durable execution checkpoint.",
                execution_id=execution_id,
            )
        return AgentRecoveryDecision(
            action=RecoveryKind.ABORT_TASK,
            reason="No recoverable task or execution state was found.",
            execution_id=execution_id,
        )

    def build_resume_context(
        self,
        *,
        task: Any | None = None,
        execution: Any | None = None,
        checkpoint: Any | None = None,
        memory_ids_used: list[str] | None = None,
        trace_context: Mapping[str, Any] | None = None,
    ) -> ResumeContext:
        decision = self.decide(task=task, execution=execution, checkpoint=checkpoint)
        snapshot = _value(checkpoint, "snapshot", {}) or {}
        if not isinstance(snapshot, Mapping):
            snapshot = {}
        steps = list(_value(execution, "steps", ()) or ())
        completed_steps = {
            str(_value(step, "step_id") or "")
            for step in steps
            if _value(step, "status") == StepStatus.COMPLETED
        }
        completed_steps.update(str(item) for item in (_value(checkpoint, "completed_steps", ()) or ()))
        goals = list(_value(task, "goals", ()) or ())
        completed_goals = [
            str(_value(goal, "goal_id") or "")
            for goal in goals
            if str(_value(goal, "status") or "").upper() in {"COMPLETED", "DONE"}
        ]
        failed = _failed_step(execution)
        artifacts = list(_value(task, "artifacts", ()) or ())
        for step in steps:
            artifact = _value(step, "output_artifact")
            if artifact is not None:
                artifacts.append(_dump(artifact))
        return ResumeContext(
            task_id=str(_value(task, "task_id") or _value(execution, "task_id") or ""),
            goal_tree_version=int(_value(task, "goal_tree_version") or 0),
            plan_version=int(_value(task, "plan_version") or 0),
            iteration=int(snapshot.get("iteration", 0) or 0),
            last_action=str(_value(task, "last_action") or snapshot.get("last_action", "")),
            last_observation_summary=str(snapshot.get("last_observation_summary", "")),
            waiting_state=str(snapshot.get("waiting_state", "")),
            memory_ids_used=list(memory_ids_used or snapshot.get("memory_ids_used", []) or []),
            completed_goal_ids=completed_goals,
            completed_step_ids=sorted(item for item in completed_steps if item),
            failed_step_id=str(_value(failed, "step_id") or ""),
            failed_error_code=str(_value(failed, "error_code") or ""),
            artifacts=[_dump(item) for item in artifacts],
            execution_id=str(_value(execution, "execution_id") or ""),
            recovery_action=decision.action,
            recovery_reason=decision.reason,
            trace_context=dict(trace_context or snapshot.get("trace_context", {}) or {}),
        )


def _value(value: Any, field: str, default: Any = "") -> Any:
    if value is None:
        return default
    if isinstance(value, Mapping):
        return value.get(field, default)
    return getattr(value, field, default)


def _failed_step(execution: Any | None) -> Any | None:
    for step in list(_value(execution, "steps", ()) or ()):
        if _value(step, "status") in {StepStatus.FAILED, StepStatus.FAILED_RETRYABLE, "FAILED", "FAILED_RETRYABLE"}:
            return step
    return None


def _dump(value: Any) -> dict[str, Any] | Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return value


__all__ = [
    "AgentRecoveryDecision",
    "AgentRecoveryService",
    "IdempotentRecoveryGuard",
    "RecoveryKind",
    "ResumeContext",
]
