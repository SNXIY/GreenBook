"""Canonical Intelligence -> Reliable Execution handoff contracts.

The objects in this module are the only business-neutral data allowed to
cross into the queue.  They contain resolved execution facts, never a user
message or a request to infer missing arguments.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ExecutionStepInput(BaseModel):
    """One resolved step in a queued execution request."""

    model_config = ConfigDict(extra="forbid")

    step_id: str
    # Preserves the logical business goal assigned by the validated plan.
    # This is projection metadata; workers still execute the canonical step.
    goal_id: str | None = None
    ordinal: int = 0
    capability: str = ""
    tool_name: str = ""
    description: str = ""
    input_artifact_types: list[str] = Field(default_factory=list)
    output_artifact_type: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    dependency_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    execution_mode: str = "QUEUE"
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    trace_context: dict[str, Any] = Field(default_factory=dict)


class ExecutionInput(BaseModel):
    """Resolved execution request consumed by Queue/Worker.

    Canonical producers populate ``steps`` and the queue payload does not
    contain natural-language understanding fields. Workers can
    reconstruct an ``ExecutablePlan`` from those resolved steps with
    :meth:`to_executable_plan`.
    """

    model_config = ConfigDict(extra="forbid")

    execution_input_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    conversation_id: str = ""
    user_id: str = ""
    tenant_id: str = ""
    goal_id: str = ""
    plan_id: str = ""
    plan_version: int = Field(default=1, ge=1)
    step_id: str = ""
    capability: str = ""
    tool_name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    dependency_refs: list[str] = Field(default_factory=list)
    execution_mode: str = "QUEUE"
    policy_snapshot: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = ""
    trace_context: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    goal: str = ""
    goal_category: str = "COMPOSITE"
    capabilities: list[str] = Field(default_factory=list)
    requirements: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    target: dict[str, Any] | None = None
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    steps: list[ExecutionStepInput] = Field(default_factory=list)

    def to_executable_plan(self) -> Any:
        """Rebuild the protected plan contract without understanding text."""

        from greenbook_agent_core.planning.contracts import PlanStep
        from greenbook_agent_core.planning.models import ExecutablePlan

        step_values = list(self.steps)
        if not step_values and self.step_id:
            step_values = [
                ExecutionStepInput(
                    step_id=self.step_id,
                    goal_id=self.goal_id or None,
                    capability=self.capability,
                    tool_name=self.tool_name,
                    arguments=dict(self.arguments),
                    constraints={
                        **{
                            str(item.get("type", "")): item.get("value")
                            for item in self.constraints
                            if isinstance(item, dict) and item.get("type")
                        },
                        **self.arguments,
                    },
                    dependency_refs=list(self.dependency_refs),
                    artifact_refs=list(self.artifact_refs),
                    execution_mode=self.execution_mode,
                    policy_snapshot=dict(self.policy_snapshot),
                    idempotency_key=self.idempotency_key,
                    trace_context=dict(self.trace_context),
                )
            ]
        plan_steps = [
            PlanStep(
                step_id=item.step_id,
                goal_id=item.goal_id,
                ordinal=item.ordinal or index + 1,
                capability=item.capability,
                tool_name=item.tool_name,
                description=item.description,
                input_artifact_types=list(item.input_artifact_types),
                output_artifact_type=item.output_artifact_type,
                depends_on=list(item.dependency_refs),
                constraints={**item.constraints, **item.arguments},
            )
            for index, item in enumerate(step_values)
        ]
        return ExecutablePlan(
            plan_id=self.plan_id,
            task_id=self.task_id,
            plan_version=self.plan_version,
            steps=plan_steps,
            is_valid=True,
            capabilities_validated=True,
            tools_mapped=all(bool(item.tool_name) for item in step_values),
            dependencies_checked=True,
            artifacts_checked=True,
            cycles_checked=True,
        )

    @classmethod
    def from_executable_plan(
        cls,
        *,
        task_id: str,
        plan: Any,
        executable: Any,
        conversation_id: str = "",
        user_id: str = "",
        tenant_id: str = "",
        goal_id: str = "",
        goal: str = "",
        goal_category: str = "COMPOSITE",
        constraints: list[dict[str, Any]] | None = None,
        target: dict[str, Any] | None = None,
        artifact_refs: list[dict[str, Any]] | None = None,
        execution_metadata: dict[str, Any] | None = None,
        trace_context: dict[str, Any] | None = None,
        policy_catalog: Any | None = None,
    ) -> ExecutionInput:
        """Compile a validated plan into the canonical queue request."""

        plan_steps = list(getattr(executable, "steps", ()) or getattr(plan, "steps", ()) or ())
        steps = [
            ExecutionStepInput(
                step_id=str(getattr(item, "step_id", "")),
                goal_id=str(getattr(item, "goal_id", "") or "") or None,
                ordinal=int(getattr(item, "ordinal", 0) or 0),
                capability=str(getattr(item, "capability", "")),
                tool_name=str(getattr(item, "tool_name", "") or ""),
                description=str(getattr(item, "description", "") or ""),
                input_artifact_types=list(getattr(item, "input_artifact_types", ()) or ()),
                output_artifact_type=str(getattr(item, "output_artifact_type", "") or ""),
                arguments=dict(getattr(item, "constraints", {}) or {}),
                constraints=dict(getattr(item, "constraints", {}) or {}),
                dependency_refs=list(getattr(item, "depends_on", ()) or ()),
                artifact_refs=[],
                execution_mode="QUEUE",
                policy_snapshot=_policy_snapshot(policy_catalog, str(getattr(item, "tool_name", "") or "")),
                idempotency_key=f"{task_id}:{getattr(plan, 'plan_id', '')}:{getattr(item, 'step_id', '')}",
                trace_context=dict(trace_context or {}),
            )
            for item in plan_steps
        ]
        capabilities = list(dict.fromkeys(item.capability for item in steps if item.capability))
        first = steps[0] if len(steps) == 1 else None
        return cls(
            task_id=task_id,
            conversation_id=conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
            goal_id=goal_id,
            plan_id=str(getattr(plan, "plan_id", "") or getattr(executable, "plan_id", "")),
            plan_version=int(getattr(plan, "plan_version", 1) or 1),
            goal=goal,
            goal_category=goal_category,
            capabilities=capabilities,
            requirements=[{"type": item} for item in capabilities],
            step_id=first.step_id if first else "",
            capability=first.capability if first else "",
            tool_name=first.tool_name if first else "",
            arguments=dict(first.arguments) if first else {},
            dependency_refs=list(first.dependency_refs) if first else [],
            execution_mode=first.execution_mode if first else "QUEUE",
            policy_snapshot=dict(first.policy_snapshot) if first else {},
            idempotency_key=first.idempotency_key if first else "",
            constraints=list(constraints or []),
            artifact_refs=list(artifact_refs or []),
            target=target,
            execution_metadata=dict(execution_metadata or {}),
            steps=steps,
            trace_context=dict(trace_context or {}),
        )


def _policy_snapshot(catalog: Any | None, tool_name: str) -> dict[str, Any]:
    """Copy policy metadata for an already-selected tool into the request."""

    if catalog is None or not tool_name:
        return {}
    value: Any = None
    getter = getattr(catalog, "get_tool_metadata", None)
    if callable(getter):
        try:
            value = getter(tool_name)
        except (KeyError, ValueError):
            value = None
    if value is None:
        getter = getattr(catalog, "get", None)
        if callable(getter):
            try:
                value = getter(tool_name)
            except (KeyError, ValueError):
                value = None
    if value is None and isinstance(catalog, dict):
        value = catalog.get(tool_name)
    if hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


__all__ = ["ExecutionInput", "ExecutionStepInput"]
