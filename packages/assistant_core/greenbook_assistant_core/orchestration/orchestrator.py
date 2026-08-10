"""Task Orchestrator — generate execution plans from TaskIntent + Capabilities.

Phase 3.1: plan generation only.  No execution.
"""

from __future__ import annotations

from greenbook_assistant_core.capability.registry import CapabilityRegistry
from greenbook_assistant_core.task.intent_models import ActionType, IntentSpec
from greenbook_assistant_core.task.models import TaskGoal, TaskIntent

from .context import PlanningContext, build_planning_context
from .agent_registry import AgentRegistry, AgentResolutionError
from .models import MultiGoalPlan, TaskPlan
from .templates import PlanTemplate, get_template


class TaskOrchestrator:
    """Generate TaskPlans from TaskIntents using community task templates.

    This is NOT a general-purpose AI planner.  It selects from a fixed
    catalog of templates based on the requirements and goal_category in
    the TaskIntent, then instantiates the chosen template into a
    validated TaskPlan.
    """

    def __init__(
        self,
        registry: CapabilityRegistry | None = None,
        agent_registry: AgentRegistry | None = None,
    ) -> None:
        self._registry = registry or CapabilityRegistry()
        self._agent_registry = agent_registry or AgentRegistry()

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
        # 5. Resolve declared Agent metadata without changing execution.
        plan = self._resolve_agents(plan)
        # 6. Validate producer/consumer schema contracts before execution.
        plan = self._validate_agent_schema_flow(plan)

        return plan

    def _resolve_agents(self, plan: TaskPlan) -> TaskPlan:
        for step in plan.steps:
            try:
                metadata = self._agent_registry.resolve_agent(
                    step.capability,
                    input_artifacts=list(step.input_artifact_types),
                    output_artifact=step.output_artifact_type,
                )
                step.agent_name = metadata.name
            except AgentResolutionError as exc:
                step.description = f"{step.description} [WARN: {exc}]"
        return plan

    def _validate_agent_schema_flow(self, plan: TaskPlan) -> TaskPlan:
        by_id = {step.step_id: step for step in plan.steps}
        for step in plan.steps:
            if not step.agent_name or not step.input_artifact_types:
                continue
            for dependency_id in step.depends_on:
                producer = by_id.get(dependency_id)
                if producer is None or not producer.agent_name or not producer.output_artifact_type:
                    continue
                if not any(
                    self._agent_registry.artifact_types_compatible(
                        producer.output_artifact_type, needed,
                    )
                    for needed in step.input_artifact_types
                ):
                    continue
                try:
                    self._agent_registry.validate_schema_contract(
                        producer.agent_name, step.agent_name,
                    )
                except AgentResolutionError as exc:
                    step.description = f"{step.description} [PLAN_VALIDATION_FAILURE: {exc}]"
        return plan

    def generate_goal_plan(
        self,
        *,
        task_id: str,
        goals: list[dict[str, object]],
        requirements: list[dict[str, str]] | None = None,
        goal_category: str = "COMPOSITE",
    ) -> MultiGoalPlan:
        """Build semantic Goal records and bind them to one existing DAG.

        A Goal is not an execution step.  The physical step catalog remains
        unchanged; e.g. ``CREATE_DRAFT`` is represented by the GENERATE step's
        DRAFT artifact, so no new ToolRuntime capability is introduced.
        """
        task_goals: list[TaskGoal] = []
        for item in goals:
            task_goals.append(TaskGoal(
                task_id=task_id,
                description=str(item.get("description", "")),
                kind=str(item.get("kind", "")),
                depends_on_goal_ids=[str(value) for value in item.get("depends_on_goal_ids", []) or []],
            ))
        inferred = requirements or [
            {"type": kind}
            for kind in (goal.kind.upper() for goal in task_goals)
            if kind in {"SEARCH", "ANALYZE", "CREATE", "GENERATE", "IMPROVE", "PUBLISH", "CANCEL"}
        ]
        aliases = {"GENERATE": "CREATE", "DRAFT": "CREATE", "SCHEDULE": "PUBLISH"}
        inferred = [{"type": aliases.get(str(item.get("type", "")).upper(), str(item.get("type", "")).upper())} for item in inferred]
        plan = self.generate_plan(
            task_id=task_id,
            goal_category=goal_category,
            requirements=inferred,
        )
        kind_to_goal: dict[str, TaskGoal] = {}
        for goal in task_goals:
            kind_to_goal.setdefault(goal.kind.upper(), goal)
        capability_kind = {
            "SEARCH_COMMUNITY": "SEARCH",
            "ANALYZE_CONTENT_PATTERNS": "ANALYZE",
            "GENERATE_CONTENT": "GENERATE",
            "IMPROVE_CONTENT": "IMPROVE",
            "SCHEDULE_PUBLISH": "PUBLISH",
            "CANCEL_SCHEDULE": "CANCEL",
        }
        for step in plan.steps:
            goal = kind_to_goal.get(capability_kind.get(step.capability, ""))
            if goal is None and step.capability == "GENERATE_CONTENT":
                goal = kind_to_goal.get("DRAFT") or kind_to_goal.get("CREATE")
            if goal is not None:
                step.goal_id = goal.goal_id
        return MultiGoalPlan(task_id=task_id, goals=task_goals, plan=plan)

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
