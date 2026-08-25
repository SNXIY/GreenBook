"""Typed contracts for the ActionLoop reasoning loop."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from greenbook_contracts.tool_result import ResourceRef


class ActionDecisionType(StrEnum):
    """The one semantic decision the loop makes each iteration."""

    CALL_TOOL = "CALL_TOOL"            # execute a deterministic semantic action
    GENERATE_CONTENT = "GENERATE_CONTENT"
    COMPOSE_RESULT = "COMPOSE_RESULT"  # build the user-facing result from ready evidence
    CLARIFY = "CLARIFY"
    WAIT = "WAIT"                      # a write is in-flight; do not reason on it
    REPLAN = "REPLAN"                  # revise/insert steps after a failure or pivot
    FINISH = "FINISH"                  # objectives satisfied by verified facts


class ActionStepPlan(BaseModel):
    """One lightweight step in an optional ActionPlan.

    A plan is an editable execution suggestion, never a business fact.  Most
    complex Tasks run without one (pure ReAct); a plan is created only when a
    multi-step/dependency/waiting shape genuinely needs it.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = ""
    objective_id: str = ""
    semantic_action: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    expected_artifact: str = ""
    expected_resource_kind: str = ""
    status: str = "PENDING"
    resource_refs: list[str] = Field(default_factory=list)


class ActionDecision(BaseModel):
    """Structured next-step decision emitted by the loop's model call."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    decision: ActionDecisionType
    reason: str = ""
    task_id: str = ""
    semantic_action: str = ""           # canonical SemanticAction, not user text
    capability: str = ""                # filled deterministically when resolvable
    tool_name: str = ""                 # filled deterministically; model may not override
    arguments: dict[str, Any] = Field(default_factory=dict)
    plan_steps: list[ActionStepPlan] = Field(default_factory=list)
    needs_clarification: bool = False


class ActionObservation(BaseModel):
    """One observed outcome of an action (tool result / durable submission)."""

    model_config = ConfigDict(extra="allow")

    iteration: int = 0
    action: str = ""                    # semantic action attempted
    tool_name: str = ""
    task_id: str = ""
    objective_id: str = ""
    query: str = ""
    input_fingerprint: str = ""
    outcome: str = "PENDING"            # SUCCESS | FAILED | SUBMITTED | RESULT_UNKNOWN | NONE
    ok: bool = False
    resource_id: str | None = None
    resource_kind: str | None = None
    resource_refs: list[ResourceRef] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    verified_facts: dict[str, Any] = Field(default_factory=dict)
    error_code: str = ""
    execution_id: str | None = None
    artifact_id: str | None = None
    message: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @property
    def verified(self) -> bool:
        return bool(self.ok and self.outcome == "SUCCESS" and self.resource_id)


class ActionLoopResult(BaseModel):
    """Terminal result of an ActionLoop run for one Task."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: str = ""                    # COMPLETED | FAILED | WAITING_HUMAN | WAITING_EXTERNAL
    task_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    success: bool = False
    content: str = ""
    iterations: int = 0
    decisions: list[str] = Field(default_factory=list)
    observations: list[ActionObservation] = Field(default_factory=list)
    plan: list[ActionStepPlan] = Field(default_factory=list)
    # Ephemeral executable-plan projection.  Objective/Resource remain the
    # business truth; this is rebuilt on resume and never owns lifecycle.
    task_plan: Any | None = None
    error_code: str = ""
    error_message: str = ""
    execution_id: str | None = None
    approval_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    partial_results: dict[str, Any] = Field(default_factory=dict)
    compose_attempts: int = 0
    final_result: Any | None = None
    # Minimal machine-readable ActionLoop progress evidence.  This is an
    # observability projection; Objective/Execution state remains canonical.
    progress_trace: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    "ActionDecision",
    "ActionDecisionType",
    "ActionObservation",
    "ActionLoopResult",
    "ActionStepPlan",
]
