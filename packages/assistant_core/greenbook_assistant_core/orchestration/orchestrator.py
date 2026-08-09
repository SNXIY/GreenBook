"""Task Orchestrator — generate execution plans from TaskIntent + Capabilities.

Phase 3.1: plan generation only.  No execution.
"""

from __future__ import annotations

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.task.intent_models import ActionType, IntentSpec
from greenbook_assistant_core.task.models import TaskIntent

from .context import PlanningContext, build_planning_context
from .models import TaskPlan
from .templates import PlanTemplate, get_template


class TaskOrchestrator:
    """Generate TaskPlans from TaskIntents using community task templates.

    This is NOT a general-purpose AI planner.  It selects from a fixed
    catalog of templates based on the requirements and goal_category in
    the TaskIntent, then instantiates the chosen template into a
    validated TaskPlan.
    """

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._registry = registry or CapabilityRegistry()

    # ── main entry ───────────────────────────────────────────────

    def generate_plan(
        self,
        *,
        task_id: str = "",
        goal_category: str = "",
        requirements: list[dict[str, str]] | None = None,
        planning_context: PlanningContext | None = None,
    ) -> TaskPlan:
        """Produce a TaskPlan for *task_id*.

        Returns a TaskPlan (possibly empty-steps) — never raises.
        Validation errors are recorded on the plan's steps.
        """
        if planning_context is not None and planning_context.intent_spec is not None:
            req_types = self._intent_spec_requirement_types(planning_context.intent_spec)
            if not goal_category:
                goal_category = str(planning_context.task_intent.goal_category)
        else:
            reqs = requirements or []
            if planning_context is not None:
                reqs = planning_context.task_intent.requirements
                if not goal_category:
                    goal_category = str(planning_context.task_intent.goal_category)
            req_types = [r.get("type", "").strip().upper() for r in reqs]

        # 1. Select template
        template = self._select_template(goal_category, req_types)

        # 2. Instantiate
        plan = template.instantiate(task_id)

        # Preserve intent constraints at the planner boundary. Conditions
        # remain in PlanningContext because they are not execution steps.
        if planning_context is not None and planning_context.intent_spec is not None:
            plan = self._apply_intent_constraints(plan, planning_context)

        # 3. Fix placeholder dependency IDs → real step_ids
        plan = self._fix_dependencies(plan)

        # 4. Validate capabilities exist in registry
        plan = self._validate_capabilities(plan)

        return plan

    @staticmethod
    def _apply_intent_constraints(
        plan: TaskPlan,
        context: PlanningContext,
    ) -> TaskPlan:
        """Attach approval/time intent constraints to the publish step."""
        intent_constraints = {
            constraint.type.value: constraint.value
            for constraint in context.intent_spec.constraints
        }
        if not intent_constraints:
            return plan

        for step in plan.steps:
            if step.capability != "SCHEDULE_PUBLISH":
                continue
            if "APPROVAL" in intent_constraints:
                step.constraints["approval"] = intent_constraints["APPROVAL"]
            if "TIME" in intent_constraints:
                step.constraints["time"] = intent_constraints["TIME"]
        return plan

    @staticmethod
    def build_planning_context(
        task_intent: TaskIntent,
        intent_spec: IntentSpec | None = None,
    ) -> PlanningContext:
        """Create the richer planner input while retaining legacy fallback."""
        return build_planning_context(task_intent, intent_spec)

    @staticmethod
    def _intent_spec_requirement_types(spec: IntentSpec) -> list[str]:
        """Adapt action names for existing template selection only."""
        aliases = {
            ActionType.UPDATE: "IMPROVE",
            ActionType.UPDATE_OR_CREATE: "CREATE",
        }
        return [
            aliases.get(action.action, action.action.value)
            for action in spec.actions
        ]

    # ── template selection ───────────────────────────────────────

    def _select_template(
        self,
        goal_category: str,
        req_types: list[str],
    ) -> PlanTemplate:
        """Pick the best template for the given goal + requirements.

        Selection rules (deterministic, ordered by priority):
        """
        has_search = "SEARCH" in req_types
        has_analyze = "ANALYZE" in req_types
        has_create = "CREATE" in req_types
        has_improve = "IMPROVE" in req_types
        has_publish = "PUBLISH" in req_types
        has_update = "UPDATE" in req_types
        has_cancel = "CANCEL" in req_types

        # ── COMPOSITE / full pipeline ──
        if has_search and has_analyze and has_create and has_publish:
            return self._require_template("FULL_PIPELINE")
        if has_search and has_analyze and has_create:
            return self._require_template("CREATE_WITH_RESEARCH")
        if has_search and has_analyze and has_improve:
            return self._require_template("IMPROVE_WITH_RESEARCH")

        # ── create + publish ──
        if has_create and has_publish:
            return self._require_template("CREATE_AND_PUBLISH")

        # ── create + improve ──
        if has_create and has_improve:
            return self._require_template("CREATE_AND_IMPROVE")

        # ── single-step ──
        if has_cancel:
            return self._require_template("SINGLE_CANCEL")
        if has_update:
            return self._require_template("SINGLE_MANAGE_SCHEDULE")
        if has_improve:
            return self._require_template("SINGLE_IMPROVE")
        if has_create:
            return self._require_template("SINGLE_CREATE")
        if has_search:
            return self._require_template("SINGLE_SEARCH")
        if has_publish:
            return self._require_template("SINGLE_PUBLISH")

        # ── category-based fallback ──
        fallback: dict[str, str] = {
            "CREATE_CONTENT":    "SINGLE_CREATE",
            "IMPROVE_CONTENT":   "SINGLE_IMPROVE",
            "ANALYZE_COMMUNITY": "SINGLE_SEARCH",
            "PUBLISH_CONTENT":   "SINGLE_PUBLISH",
            "MANAGE_SCHEDULE":   "SINGLE_CANCEL",
            "COMPOSITE":         "FULL_PIPELINE",
        }
        name = fallback.get(goal_category, "SINGLE_CREATE")
        return self._require_template(name)

    # ── post-processing ──────────────────────────────────────────

    @staticmethod
    def _fix_dependencies(plan: TaskPlan) -> TaskPlan:
        """Replace placeholder dep IDs (_dep_0, _dep_1) with real step_ids."""
        step_ids = [s.step_id for s in plan.steps]
        for i, step in enumerate(plan.steps):
            fixed: list[str] = []
            for dep in step.depends_on:
                if dep.startswith("_dep_"):
                    try:
                        idx = int(dep.split("_")[-1])
                        if 0 <= idx < len(step_ids):
                            fixed.append(step_ids[idx])
                    except (ValueError, IndexError):
                        pass
                else:
                    fixed.append(dep)
            step.depends_on = fixed
        return plan

    def _validate_capabilities(self, plan: TaskPlan) -> TaskPlan:
        """Check every step's capability exists in the registry."""
        for step in plan.steps:
            cap = self._registry.get(step.capability)
            if cap is None:
                step.description = (
                    f"{step.description} [WARN: unknown capability "
                    f"'{step.capability}']"
                )
        return plan

    @staticmethod
    def _require_template(name: str) -> PlanTemplate:
        tmpl = get_template(name)
        if tmpl is None:
            raise ValueError(f"Template '{name}' not found")
        return tmpl

    # ── info ─────────────────────────────────────────────────────

    @property
    def template_count(self) -> int:
        from .templates import ALL_TEMPLATES
        return len(ALL_TEMPLATES)
