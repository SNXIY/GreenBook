"""PlanValidator — pre-execution checks for TaskPlan → ExecutablePlan.

Phase 3.2: validation only — no execution.
"""

from __future__ import annotations

from greenbook_contracts.tool_contract import (
    TOOL_POLICY_CATALOG,
    ToolMetadata,
    ToolPolicyMetadata,
)

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.planning.contracts import TaskPlan

from .models import ExecutablePlan


class PlanValidator:
    """Validate a TaskPlan against the CapabilityRegistry.

    Six checks, run in order.  Each check populates *plan.errors* when
    it finds a problem but never raises — the caller inspects
    ``plan.is_valid`` to decide whether to proceed.
    """

    def __init__(self, registry: CapabilityRegistry, tool_registry: object | None = None) -> None:
        self._registry = registry
        self._tool_registry = tool_registry

    # ── main entry ───────────────────────────────────────────────

    def validate(self, task_plan: TaskPlan) -> ExecutablePlan:
        plan = ExecutablePlan(
            plan_id=task_plan.plan_id,
            task_id=task_plan.task_id,
            plan_source=task_plan.plan_source,
            plan_version=task_plan.plan_version,
            steps=[s.model_copy(deep=True) for s in task_plan.steps],
        )

        # 1. Capability existence
        self._check_capabilities_exist(plan)
        plan.capabilities_validated = True

        # 2. Tool mapping
        self._check_tool_mapping(plan)
        plan.tools_mapped = True

        # 3. Cycle detection (must run before dependency check)
        self._check_cycles(plan)
        plan.cycles_checked = True

        # 4. Step dependency satisfaction
        self._check_dependencies(plan)
        plan.dependencies_checked = True

        # 5. Artifact flow
        self._check_artifact_flow(plan)
        plan.artifacts_checked = True

        # 6. Approval requirements
        self._check_approval(plan)

        plan.is_valid = len(plan.errors) == 0
        return plan

    # ── check 1: capabilities exist ──────────────────────────────

    def _check_capabilities_exist(self, plan: ExecutablePlan) -> None:
        for step in plan.steps:
            cap = self._registry.get(step.capability)
            if cap is None:
                plan.add_error(
                    step,
                    "UNKNOWN_CAPABILITY",
                    f"Capability '{step.capability}' is not registered",
                )

    # ── check 2: tool mapping ────────────────────────────────────

    def _check_tool_mapping(self, plan: ExecutablePlan) -> None:
        for step in plan.steps:
            cap = self._registry.get(step.capability)
            if cap is None:
                continue  # already reported in check 1
            # LLM steps have no tools — that's fine
            if cap.is_llm_step:
                step.description = f"[LLM] {step.description}"
                continue
            # Non-LLM steps must have at least one tool
            if not cap.tools:
                plan.add_error(
                    step,
                    "MISSING_TOOL",
                    f"Capability '{step.capability}' has no tool mapping "
                    f"and is not marked as LLM step",
                )

    # ── check 3: cycle detection ─────────────────────────────────

    @staticmethod
    def _check_cycles(plan: ExecutablePlan) -> None:
        step_ids = {s.step_id for s in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    continue  # reported in check 4
                # Check for self-loop
                if dep == step.step_id:
                    plan.add_error(
                        step,
                        "CYCLIC_DEPENDENCY",
                        f"Step {step.ordinal} depends on itself",
                    )
                    continue
                # DFS from dep to see if it reaches this step
                if _reaches(plan, dep, step.step_id):
                    plan.add_error(
                        step,
                        "CYCLIC_DEPENDENCY",
                        f"Cycle detected: step {step.ordinal} → "
                        f"(depends on) → step with id {dep} → … → "
                        f"step {step.ordinal}",
                    )

    # ── check 4: dependencies exist ──────────────────────────────

    @staticmethod
    def _check_dependencies(plan: ExecutablePlan) -> None:
        step_ids = {s.step_id for s in plan.steps}
        for step in plan.steps:
            for dep in step.depends_on:
                if dep not in step_ids:
                    plan.add_error(
                        step,
                        "MISSING_DEPENDENCY",
                        f"Step {step.ordinal} depends on unknown "
                        f"step '{dep}'",
                    )

    # ── check 5: artifact flow ───────────────────────────────────

    @staticmethod
    def _check_artifact_flow(plan: ExecutablePlan) -> None:
        # Build a map of what each step produces
        produced: dict[str, set[str]] = {}  # step_id → {artifact_types}
        for s in plan.steps:
            if s.output_artifact_type:
                produced[s.step_id] = {s.output_artifact_type}
            else:
                produced[s.step_id] = set()

        # Set of all artifact types produced *anywhere* in this plan
        all_produced: set[str] = set()
        for types in produced.values():
            all_produced.update(types)

        for step in plan.steps:
            for needed in step.input_artifact_types:
                # Skip when the artifact is external:
                # a) not produced anywhere in this plan, OR
                # b) this step itself produces it (e.g. IMPROVE step
                #    needs DRAFT as input and produces DRAFT as output)
                if needed not in all_produced or needed in produced.get(step.step_id, set()):
                    continue

                # Check if any transitive upstream step produces this type.
                if _artifact_available(plan, produced, step.step_id, needed):
                    continue

                upstream = ", ".join(
                    f"{s.ordinal}({s.output_artifact_type or 'none'})"
                    for s in plan.steps
                    if s.step_id in step.depends_on
                )
                plan.add_error(
                    step,
                    "ARTIFACT_MISSING",
                    f"Step {step.ordinal} needs artifact "
                    f"'{needed}' but no upstream step produces it "
                    f"(direct upstream: [{upstream}])",
                )

    # ── check 6: approval requirements ───────────────────────────

    def _check_approval(self, plan: ExecutablePlan) -> None:
        for step in plan.steps:
            cap = self._registry.get(step.capability)
            if cap is None:
                continue
            policy = self._policy_for_step(step, cap)
            requires_approval = bool(policy and policy.requires_approval)
            has_side_effect = bool(policy and policy.side_effect.has_side_effect)
            if requires_approval:
                plan.requires_approval = True
                step.description = f"[APPROVAL REQUIRED] {step.description}"
            if has_side_effect:
                plan.has_side_effects = True

    def _policy_for_step(self, step, capability) -> ToolPolicyMetadata | None:
        """Resolve one tool policy from the registry or canonical catalog."""

        metadata = self._metadata_for_step(step, capability)
        if metadata is not None:
            return metadata.policy

        name = self._tool_name_for_step(step, capability)
        return TOOL_POLICY_CATALOG.get(name) if name else None

    @staticmethod
    def _tool_name_for_step(step, capability) -> str:
        """Resolve a single semantic tool without choosing from a composite."""

        selected = str(getattr(step, "tool_name", "") or "")
        if selected:
            return selected
        return next(iter(capability.tools)) if len(capability.tools) == 1 else ""

    def _metadata_for_step(self, step, capability) -> ToolMetadata | None:
        """Resolve discovery metadata without selecting a tool by position."""

        registry = self._tool_registry
        if registry is None:
            return None
        name = self._tool_name_for_step(step, capability)
        if not name:
            return None
        getter = getattr(registry, "get_tool_metadata", None)
        if callable(getter):
            try:
                value = getter(name)
            except (KeyError, ValueError):
                value = None
        else:
            getter = getattr(registry, "get", None)
            value = getter(name) if callable(getter) else None
        if value is None:
            return None
        return value if isinstance(value, ToolMetadata) else ToolMetadata.model_validate(value)


# ── helpers ──────────────────────────────────────────────────────────

def _artifact_available(
    plan: ExecutablePlan,
    produced: dict[str, set[str]],
    step_id: str,
    needed: str,
) -> bool:
    """Check if *needed* artifact is produced by any transitive upstream step.

    Walks the dependency chain *upward* from *step_id* through all
    depends_on edges, checking each ancestor's output.
    """
    visited: set[str] = set()

    def dfs(current: str) -> bool:
        if current in visited:
            return False
        visited.add(current)
        if needed in produced.get(current, set()):
            return True
        for s in plan.steps:
            if s.step_id == current:
                for dep in s.depends_on:
                    if dfs(dep):
                        return True
                break
        return False

    # Start DFS from each direct dependency
    step = next((s for s in plan.steps if s.step_id == step_id), None)
    if step is None:
        return False
    return any(dfs(dep) for dep in step.depends_on)


def _reaches(plan: ExecutablePlan, from_id: str, target_id: str) -> bool:
    """True when a path exists from *from_id* to *target_id* via depends_on."""
    visited: set[str] = set()

    def dfs(current: str) -> bool:
        if current == target_id:
            return True
        if current in visited:
            return False
        visited.add(current)
        for s in plan.steps:
            if s.step_id == current:
                for dep in s.depends_on:
                    if dfs(dep):
                        return True
        return False

    return dfs(from_id)
