"""Compile GoalTree into the existing semantic graph and TaskPlan contracts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from greenbook_agent_core.capability.registry import CapabilityRegistry
from greenbook_agent_core.command.models import Command
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan
from greenbook_agent_core.planning.graph import (
    PlanGraph,
    PlanNode,
)

from .models import Goal, GoalTree, TaskNode


class GoalCompilationError(ValueError):
    """Raised when a GoalTree cannot be mapped to the legacy DAG contract."""


_SHARED_COMMAND_FIELDS = frozenset({
    # These values can safely apply to every logical Goal.  Target, action,
    # publication mode, and temporal values are intentionally excluded: they
    # belong to an individual Goal in a multi-goal request.
    "audience",
    "format",
    "language",
    "locale",
    "requires_approval",
    "style",
    "timezone",
    "tone",
})

_PUBLICATION_INTENT_ALIASES = (
    "publication_intent",
    "publication_mode",
    "publish_mode",
    "publish_intent",
)

_TEMPORAL_ALIASES = (
    "run_at",
    "publish_at",
    "scheduled_at",
    "publish_time",
    "schedule_time",
    "time",
    "datetime",
)

# A resource binding is represented independently from one concrete Tool.  At
# the Tool boundary it needs the canonical field accepted by the resource's
# schema.  Keeping this mapping here makes Goal/Plan compilation deterministic
# without making the compiler infer a business action from natural language.
_RESOURCE_ARGUMENT_FIELDS = {
    "DRAFT": "draft_id",
    "SCHEDULE": "schedule_id",
    "POST": "post_id",
}


class GoalCompiler:
    """Compile semantic Goals without changing Worker or Execution contracts."""

    def __init__(self, registry: CapabilityRegistry | None = None) -> None:
        self._registry = registry

    def compile(
        self,
        goal_tree: GoalTree,
        *,
        command: Command | None = None,
    ) -> PlanGraph:
        """Compile executable leaf Goals into the existing PlanGraph."""

        self._validate(goal_tree)
        goals = goal_tree.executable_goals()
        self._validate_goal_semantics(goals, command=command)
        # A TASK_DELTA tree was carved out of a multi-task turn.  Its Goals
        # already own their semantic facts, so request-global target/time
        # hints must not leak into this independent plan merely because it
        # happens to contain one executable Goal.
        owned_command = (
            None
            if str(getattr(goal_tree, "source", "") or "").upper() == "TASK_DELTA"
            else command
        )
        executable_ids = {goal.goal_id for goal in goals}
        nodes: list[PlanNode] = []
        for goal in goals:
            capabilities = list(goal.required_capabilities)
            if not capabilities:
                capabilities = [self._capability_for_goal_type(goal.goal_type)]
            dependencies = [
                dependency
                for dependency in goal.dependencies
                if dependency in executable_ids and dependency != goal.goal_id
            ]
            outputs = list(goal.expected_outputs)
            if not outputs:
                outputs = [
                    output
                    for output in (self._output_for_capability(capability) for capability in capabilities)
                    if output
                ]
            node = PlanNode(
                node_id=goal.goal_id,
                text=goal.description,
                goal=goal.description,
                capabilities=capabilities,
                constraints=[
                    *[
                        dict(item)
                        for item in goal.constraints
                        if isinstance(item, Mapping)
                    ],
                    *self._typed_constraint_items(goal),
                ],
                target_hint=self._target_hint_for_goal(
                    goal,
                    command=owned_command,
                    goal_count=len(goals),
                ),
                depends_on=dependencies,
                read_only=all(
                    str(goal.goal_type).strip().upper()
                    in {"QUERY", "RESEARCH", "ANALYZE"}
                    for _ in capabilities
                ),
                create_task=str(goal.goal_type).strip().upper()
                not in {"QUERY", "RESEARCH", "ANALYZE"},
                artifact_inputs=self._artifact_inputs(goal),
                artifact_outputs=outputs,
            )
            nodes.append(node)

        edges = [
            (dependency, node.node_id)
            for node in nodes
            for dependency in node.depends_on
        ]
        graph = PlanGraph(nodes=nodes, edges=edges)
        try:
            graph.validate_graph()
        except (ValidationError, ValueError) as exc:
            raise GoalCompilationError(
                "GoalTree could not be compiled into TaskGraph."
            ) from exc
        return graph

    def compile_plan(
        self,
        goal_tree: GoalTree,
        *,
        task_id: str = "",
        plan_version: int = 1,
        previous_plan_id: str | None = None,
        change_reason: str = "",
        command: Command | None = None,
    ) -> TaskPlan:
        """Compile GoalTree into the existing TaskPlan/PlanStep shape."""

        self._validate(goal_tree)
        executable_goals = goal_tree.executable_goals()
        self._validate_goal_semantics(executable_goals, command=command)
        merge_command_context = (
            str(getattr(goal_tree, "source", "") or "").upper() != "TASK_DELTA"
        )
        goals = {goal.goal_id: goal for goal in goal_tree.all_goals()}
        # ``task_nodes`` is an optional planner hint in the GoalTree contract.
        # A structured LLM response may provide nodes for only one leaf while
        # still describing the remaining leaf capabilities correctly.  Never
        # let that partial hint silently truncate the durable execution plan:
        # reconcile it with every executable Goal before crossing into the
        # TaskPlan contract.
        task_nodes = self._complete_task_nodes(goal_tree)
        task_ids_by_goal: dict[str, list[str]] = {}
        for task in task_nodes:
            task_ids_by_goal.setdefault(task.goal_id, []).append(task.task_id)

        steps: list[PlanStep] = []
        step_ids = {task.task_id for task in task_nodes}
        for ordinal, task in enumerate(task_nodes, start=1):
            goal = goals.get(task.goal_id)
            if goal is None:
                raise GoalCompilationError(
                    f"TaskNode '{task.task_id}' references unknown goal '{task.goal_id}'."
                )
            dependencies = self._task_dependencies(
                task,
                goal,
                task_ids=step_ids,
                task_ids_by_goal=task_ids_by_goal,
            )
            capability = self._registry.get(task.capability) if self._registry else None
            output_type = (
                capability.output_artifact_type
                if capability is not None
                else (goal.expected_outputs[0] if goal.expected_outputs else "")
            )
            input_types = self._input_artifact_types(task, dependencies, steps)
            constraints = self._constraints_for_goal(
                goal,
                capability=capability,
                command=command,
                goal_count=len(executable_goals),
                task_inputs=task.inputs,
                merge_command_context=merge_command_context,
            )
            steps.append(PlanStep(
                step_id=task.task_id,
                ordinal=ordinal,
                capability=task.capability,
                tool_name=(
                    next(iter(capability.tools), "")
                    if capability is not None and len(capability.tools) == 1
                    else ""
                ),
                description=goal.description,
                depends_on=dependencies,
                input_artifact_types=input_types,
                output_artifact_type=output_type,
                parallelizable=bool(capability.parallelizable) if capability else False,
                constraints=constraints,
                goal_id=goal.goal_id,
            ))
        planned_goal_ids = {
            str(step.goal_id)
            for step in steps
            if str(step.goal_id or "")
        }
        missing_goal_ids = {
            goal.goal_id for goal in executable_goals
        } - planned_goal_ids
        if missing_goal_ids:
            raise GoalCompilationError(
                "PLAN_GOAL_COVERAGE_REQUIRED: TaskPlan is missing one or more "
                "executable Goals."
            )
        return TaskPlan(
            task_id=task_id,
            steps=steps,
            plan_source="GOAL_RUNTIME",
            plan_version=max(1, plan_version),
            previous_plan_id=previous_plan_id,
            change_reason=change_reason,
        )

    def materialize_task_nodes(self, goal_tree: GoalTree) -> GoalTree:
        """Persist the deterministic TaskNode projection before AgentLoop runs.

        ``task_nodes`` is an optional structured-output hint, but AgentLoop and
        durable Task projections need the completed semantic node set to bind
        every action to its owning Goal. Plan compilation already owns this
        completion rule; expose the same rule at the Task binding boundary so a
        missing optional hint cannot erase Goal identity from runtime state.
        """

        self._validate(goal_tree)
        goal_tree.task_nodes = self._complete_task_nodes(goal_tree)
        return goal_tree

    def to_plan_graph(self, goal_tree: GoalTree, *, command: Command | None = None) -> PlanGraph:
        return self.compile(goal_tree, command=command)

    def to_task_plan(
        self,
        goal_tree: GoalTree,
        *,
        task_id: str = "",
        command: Command | None = None,
    ) -> TaskPlan:
        return self.compile_plan(goal_tree, task_id=task_id, command=command)

    def _constraints_for_goal(
        self,
        goal: Goal,
        *,
        capability: Any | None,
        command: Command | None,
        goal_count: int = 1,
        task_inputs: Mapping[str, Any] | None = None,
        merge_command_context: bool = True,
    ) -> dict[str, Any]:
        """Compile structured Goal/Command values into tool input fields.

        Goal descriptions and constraints are already the structured output of
        the intelligence boundary.  This adapter makes the small semantic
        fields required by a Tool contract explicit before the plan crosses
        into Reliable Execution; the Worker still performs the final schema
        validation.
        """

        values = self._values_for_goal(goal)
        if command is not None and merge_command_context:
            for source_name in ("parameters", "entities", "constraints"):
                source = getattr(command, source_name, {}) or {}
                if isinstance(source, Mapping):
                    for key, value in source.items():
                        normalized = str(key).strip().lower()
                        if goal_count <= 1 or normalized in _SHARED_COMMAND_FIELDS:
                            values.setdefault(normalized, value)
            if goal_count <= 1 and command.references and "references" not in values:
                values["references"] = list(command.references)

        # A TaskNode is the most specific semantic input for one compiled
        # step.  Replans and explicit planner nodes use this field to carry a
        # corrected argument (for example a schedule time); it must override
        # only this Goal's values and must never become request-global state.
        for key, value in (task_inputs or {}).items():
            if value not in (None, "", []):
                values[str(key).strip().lower()] = value

        self._normalize_semantic_aliases(values)

        # TargetResolver may emit one structured reference object instead of
        # the older {type, value} constraint form.  Convert a *bound* resource
        # reference into the matching Tool field.  ``kind`` is required before
        # treating ``id`` as a resource id, so a Goal/Task identifier can never
        # accidentally become a draft or schedule argument.
        if "run_at" not in values and values.get("publish_at"):
            values["run_at"] = values["publish_at"]
        resource_kind = str(
            values.get("resource_kind") or values.get("kind") or ""
        ).upper()
        resource_field = _RESOURCE_ARGUMENT_FIELDS.get(resource_kind)
        resource_id = values.get("resource_id")
        if resource_id in (None, "") and resource_kind:
            resource_id = values.get("id")
        if (
            resource_field
            and resource_field not in values
            and resource_id not in (None, "")
        ):
            values[resource_field] = resource_id

        required = list(getattr(getattr(capability, "inputs", None), "required", ()) or ())
        for field in required:
            if field in values and values[field] not in (None, "", []):
                continue
            derived = self._derive_required_value(field, values, goal)
            if derived is not None:
                values[field] = derived

        self._validate_step_semantics(
            goal,
            values,
            capability_name="",
            capabilities=list(dict.fromkeys([
                *list(goal.required_capabilities),
                str(getattr(capability, "name", "") or ""),
            ])),
            command=command,
            goal_count=goal_count,
        )

        # A child Goal may intentionally describe only the current operation
        # (for example, "write the first article") while the Command carries
        # the topic, audience, and other user-level constraints. Preserve that
        # semantic context when crossing into content generation; otherwise a
        # valid multi-goal decomposition can lose the subject between planning
        # and content generation. This is context propagation, not a workflow
        # or capability mapping.
        capability_name = str(getattr(capability, "name", "")).upper()
        if capability_name == "GENERATE_CONTENT" and command is not None and goal_count <= 1:
            overall_goal = str(command.goal or command.objective or "").strip()
            instruction = str(values.get("instruction") or "").strip()
            if overall_goal and instruction and overall_goal not in instruction:
                values["instruction"] = (
                    f"{instruction}\n\n"
                    "Overall user objective context (preserve its topic and "
                    "audience; execute only this content-generation step): "
                    f"{overall_goal}"
                )
        return values

    def _values_for_goal(self, goal: Goal) -> dict[str, Any]:
        """Return semantic values owned by one Goal before command merging."""

        values = self._constraints(goal.constraints)
        target = getattr(goal, "target", {}) or {}
        if isinstance(target, Mapping):
            for key, value in target.items():
                if value not in (None, "", []):
                    values.setdefault(str(key).strip().lower(), value)

        temporal = getattr(goal, "temporal_constraint", {}) or {}
        if isinstance(temporal, Mapping):
            # Retain the structured container long enough for
            # ``_normalize_semantic_aliases`` to recognise neutral keys such
            # as ``expression`` or ``when`` as the Goal's run_at value.
            values.setdefault("temporal_constraint", dict(temporal))
            for key, value in temporal.items():
                if value not in (None, "", []):
                    values.setdefault(str(key).strip().lower(), value)
        elif temporal not in (None, "", []):
            values.setdefault("run_at", temporal)

        semantic_operation = str(getattr(goal, "semantic_operation", "") or "").strip()
        if semantic_operation:
            values["semantic_operation"] = semantic_operation
        publication_intent = str(getattr(goal, "publication_intent", "") or "").strip()
        if publication_intent:
            values["publication_intent"] = publication_intent
        self._normalize_semantic_aliases(values)
        return values

    @staticmethod
    def _typed_constraint_items(goal: Goal) -> list[dict[str, Any]]:
        """Expose typed Goal semantics to the compatibility PlanGraph."""

        items: list[dict[str, Any]] = []
        if goal.semantic_operation:
            items.append({"type": "semantic_operation", "value": goal.semantic_operation})
        if goal.publication_intent:
            items.append({"type": "publication_intent", "value": goal.publication_intent})
        if goal.target:
            items.append({"type": "target", "value": dict(goal.target)})
        if goal.temporal_constraint:
            items.append({"type": "temporal_constraint", "value": dict(goal.temporal_constraint)})
        return items

    @staticmethod
    def _target_hint_for_goal(
        goal: Goal,
        *,
        command: Command | None,
        goal_count: int,
    ) -> str | None:
        target = getattr(goal, "target", {}) or {}
        if isinstance(target, Mapping):
            for key in ("reference", "id", "resource_id", "value", "title", "name"):
                value = target.get(key)
                if value not in (None, ""):
                    return str(value)
        if goal_count == 1 and command is not None and command.target is not None:
            return (
                command.target.reference
                or command.target.explicit_id
                or command.target.value
            )
        return None

    def _validate_goal_semantics(
        self,
        goals: list[Goal],
        *,
        command: Command | None,
    ) -> None:
        """Fail closed before a plan can turn ambiguous semantics into side effects."""

        goal_count = len(goals)
        for goal in goals:
            values = self._values_for_goal(goal)
            if command is not None and goal_count <= 1:
                for source_name in ("parameters", "entities", "constraints"):
                    source = getattr(command, source_name, {}) or {}
                    if isinstance(source, Mapping):
                        for key, value in source.items():
                            values.setdefault(str(key).strip().lower(), value)
            self._normalize_semantic_aliases(values)
            capabilities = list(goal.required_capabilities) or [
                self._capability_for_goal_type(goal.goal_type)
            ]
            for capability in capabilities:
                if str(capability).upper() == "SCHEDULE_PUBLISH":
                    derived = self._derive_required_value("run_at", values, goal)
                    if derived is not None:
                        values.setdefault("run_at", derived)
            self._validate_step_semantics(
                goal,
                values,
                capability_name="",
                capabilities=capabilities,
                command=command,
                goal_count=goal_count,
            )

    def _validate_step_semantics(
        self,
        goal: Goal,
        values: Mapping[str, Any],
        *,
        capability_name: str,
        capabilities: list[str] | None = None,
        command: Command | None,
        goal_count: int = 1,
    ) -> None:
        caps = {
            str(value).strip().upper()
            for value in (capabilities or ([capability_name] if capability_name else []))
            if str(value).strip()
        }
        intent = self._publication_intent(values)
        run_at = self._first_value(values, _TEMPORAL_ALIASES)
        has_schedule = "SCHEDULE_PUBLISH" in caps
        has_publish_now = "PUBLISH_NOW" in caps

        if intent == "DRAFT_ONLY" and (has_schedule or has_publish_now or run_at not in (None, "", [])):
            raise GoalCompilationError(
                f"Goal '{goal.goal_id}' is DRAFT_ONLY but contains publication side effects."
            )
        if intent == "SCHEDULED_PUBLISH" and has_publish_now:
            raise GoalCompilationError(
                f"Goal '{goal.goal_id}' is scheduled but contains PUBLISH_NOW."
            )
        if intent == "IMMEDIATE_PUBLISH" and not has_publish_now:
            raise GoalCompilationError(
                f"Goal '{goal.goal_id}' requests immediate publication without PUBLISH_NOW."
            )
        if intent == "SCHEDULED_PUBLISH" and not has_schedule:
            raise GoalCompilationError(
                f"Goal '{goal.goal_id}' requests scheduled publication without SCHEDULE_PUBLISH."
            )
        if has_schedule and intent == "IMMEDIATE_PUBLISH":
            raise GoalCompilationError(
                f"Goal '{goal.goal_id}' maps immediate publication to SCHEDULE_PUBLISH."
            )
        if has_publish_now and goal_count > 1 and not self._is_explicit_immediate(values):
            raise GoalCompilationError(
                f"Goal '{goal.goal_id}' must explicitly declare IMMEDIATE_PUBLISH "
                "before a multi-goal request can publish immediately."
            )
        if has_publish_now and command is not None:
            requested = {
                str(value).strip().upper()
                for value in command.required_capabilities
            }
            if "PUBLISH_NOW" not in requested:
                raise GoalCompilationError(
                    f"Goal '{goal.goal_id}' introduced unrequested PUBLISH_NOW."
                )

    @classmethod
    def _is_explicit_immediate(cls, values: Mapping[str, Any]) -> bool:
        """Return whether this Goal, not the aggregate Command, asks to publish now."""

        if cls._publication_intent(values) == "IMMEDIATE_PUBLISH":
            return True
        operation = str(values.get("semantic_operation") or "").strip().upper()
        return operation in {"IMMEDIATE_PUBLISH", "PUBLISH_NOW", "IMMEDIATE"}

    @staticmethod
    def _publication_intent(values: Mapping[str, Any]) -> str:
        for key in _PUBLICATION_INTENT_ALIASES:
            value = values.get(key)
            if value not in (None, "", []):
                normalized = str(value).strip().upper().replace("-", "_").replace(" ", "_")
                aliases = {
                    "DRAFT": "DRAFT_ONLY",
                    "SAVE_DRAFT": "DRAFT_ONLY",
                    "DO_NOT_PUBLISH": "DRAFT_ONLY",
                    "NO_PUBLISH": "DRAFT_ONLY",
                    "SCHEDULE": "SCHEDULED_PUBLISH",
                    "SCHEDULE_PUBLISH": "SCHEDULED_PUBLISH",
                    "IMMEDIATE": "IMMEDIATE_PUBLISH",
                    "PUBLISH_NOW": "IMMEDIATE_PUBLISH",
                    "NOW": "IMMEDIATE_PUBLISH",
                }
                return aliases.get(normalized, normalized)
        return ""

    @staticmethod
    def _first_value(values: Mapping[str, Any], aliases: tuple[str, ...]) -> Any | None:
        for alias in aliases:
            value = values.get(alias)
            if value not in (None, "", []):
                return value
        return None

    @staticmethod
    def _normalize_semantic_aliases(values: dict[str, Any]) -> None:
        """Flatten structured temporal/target aliases into Tool contract fields."""

        for alias in ("temporal", "temporal_constraint", "time_constraint"):
            nested = values.get(alias)
            if isinstance(nested, Mapping):
                for key, value in nested.items():
                    if value not in (None, "", []):
                        values.setdefault(str(key).strip().lower(), value)
                if "run_at" not in values:
                    for key in ("expression", "value", "at", "when"):
                        value = nested.get(key)
                        if value not in (None, "", []):
                            values["run_at"] = value
                            break
            elif nested not in (None, "", []):
                values.setdefault("run_at", nested)
        if "run_at" not in values and values.get("publish_at"):
            values["run_at"] = values["publish_at"]
        if "run_at" not in values and values.get("scheduled_at"):
            values["run_at"] = values["scheduled_at"]

    @staticmethod
    def _derive_required_value(
        field: str,
        values: Mapping[str, Any],
        goal: Goal,
    ) -> Any | None:
        aliases: dict[str, tuple[str, ...]] = {
            "title": ("title", "headline", "subject", "topic"),
            "instruction": ("instruction", "brief", "prompt", "goal", "description"),
            "revision_instruction": (
                "revision_instruction",
                "revision",
                "instruction",
                "brief",
                "prompt",
                "goal",
                "description",
            ),
            "query": ("query", "keywords", "keyword", "topic"),
            "run_at": (
                "run_at",
                "publish_at",
                "scheduled_at",
                "publish_time",
                "schedule_time",
                "time",
                "datetime",
            ),
            "draft_id": ("draft_id", "resource_id", "id"),
        }
        for alias in aliases.get(field, ()):
            value = values.get(alias)
            if value not in (None, "", []):
                return value
        if field == "run_at":
            return GoalCompiler._extract_datetime(goal.description)
        if field in {"title", "instruction", "revision_instruction"} and goal.description.strip():
            return goal.description.strip()
        return None

    @staticmethod
    def _extract_datetime(description: str) -> str | None:
        """Normalize an ISO-like datetime already present in Goal facts.

        This is a contract adapter, not intent routing: it only accepts a
        date/time emitted by the structured Goal boundary. Natural-language
        interpretation remains the responsibility of Command/Goal LLMs.
        """

        text = str(description or "")
        match = re.search(
            r"(?P<date>20\d{2}-\d{1,2}-\d{1,2})"
            r"(?:[T\s]+(?P<time>\d{1,2}:\d{2})(?::\d{2})?"
            r"(?P<offset>Z|[+-]\d{2}:?\d{2})?)?",
            text,
        )
        if match is None or not match.group("time"):
            return None
        date = match.group("date")
        hour, minute = match.group("time").split(":", 1)
        offset = match.group("offset")
        if offset == "Z":
            suffix = "+00:00"
        elif offset:
            suffix = offset if ":" in offset else f"{offset[:3]}:{offset[3:]}"
        elif "北京时间" in text or "Asia/Shanghai" in text:
            suffix = "+08:00"
        else:
            suffix = ""
        return f"{date}T{int(hour):02d}:{int(minute):02d}:00{suffix}"

    def _validate(self, goal_tree: GoalTree) -> None:
        if not isinstance(goal_tree, GoalTree):
            raise GoalCompilationError("GoalCompiler requires a GoalTree.")
        try:
            goal_tree.validate_tree()
        except ValueError as exc:
            raise GoalCompilationError(str(exc)) from exc

    def _derive_task_nodes(self, goal_tree: GoalTree) -> list[TaskNode]:
        result: list[TaskNode] = []
        for goal in goal_tree.executable_goals():
            capabilities = goal.required_capabilities or [
                self._capability_for_goal_type(goal.goal_type)
            ]
            for index, capability in enumerate(capabilities, start=1):
                result.append(TaskNode(
                    task_id=f"{goal.goal_id}:{index}",
                    goal_id=goal.goal_id,
                    capability=capability,
                    dependencies=list(goal.dependencies),
                ))
        return result

    def _complete_task_nodes(self, goal_tree: GoalTree) -> list[TaskNode]:
        """Complete optional planner hints without inventing a workflow.

        Goal.required_capabilities remains the semantic source of truth.  An
        explicit TaskNode is preserved as-is; only capabilities missing for an
        executable leaf Goal are materialized as deterministic TaskNodes.  A
        later capability on the same Goal depends on the previous materialized
        capability, while inter-Goal dependencies continue to be resolved by
        ``_task_dependencies``.
        """

        existing = list(goal_tree.task_nodes)
        by_goal: dict[str, list[TaskNode]] = {}
        used_ids = {task.task_id for task in existing}
        for task in existing:
            by_goal.setdefault(task.goal_id, []).append(task)

        for goal in goal_tree.executable_goals():
            capabilities = list(goal.required_capabilities) or [
                self._capability_for_goal_type(goal.goal_type)
            ]
            present = {task.capability for task in by_goal.get(goal.goal_id, [])}
            missing = [capability for capability in capabilities if capability not in present]
            for index, capability in enumerate(missing, start=1):
                task_id = f"{goal.goal_id}:{index}"
                while task_id in used_ids:
                    index += 1
                    task_id = f"{goal.goal_id}:{index}"
                # Preserve the established deterministic chain for missing
                # planner hints, but derive it from the Goal's declared
                # capability order rather than from whichever partial node
                # happened to arrive first.
                capability_index = capabilities.index(capability)
                predecessor_ids = [
                    task.task_id
                    for previous_capability in reversed(capabilities[:capability_index])
                    for task in by_goal.get(goal.goal_id, [])
                    if str(task.capability).strip().upper()
                    == str(previous_capability).strip().upper()
                ]
                task = TaskNode(
                    task_id=task_id,
                    goal_id=goal.goal_id,
                    capability=capability,
                    dependencies=list(dict.fromkeys([
                        *goal.dependencies,
                        *predecessor_ids[:1],
                    ])),
                )
                existing.append(task)
                by_goal.setdefault(goal.goal_id, []).append(task)
                used_ids.add(task_id)

        # A partial structured response may name a publication step while
        # omitting the preceding content-generation node.  In a multi-goal
        # request that must not leave publication to resolve a request-global
        # "latest draft".  Link a publication step to the single draft it can
        # unambiguously own; when ownership is ambiguous, validation below
        # rejects the plan rather than guessing.
        goals_by_id = {goal.goal_id: goal for goal in goal_tree.executable_goals()}

        # Deterministic evidence-bounded analysis: a Goal that searches the
        # community and then ANALYZEs must first read the actual post bodies
        # (design goal 0813 — a summary derived only from search-result titles
        # fabricates depth).  Insert a GET_POST_DETAIL node between
        # SEARCH_COMMUNITY and ANALYZE_CONTENT_PATTERNS when the Goal declares
        # both but no read step; post_id injection is resolved later from the
        # search evidence.  This is a system guarantee, not a model hint.
        for goal_id, tasks in by_goal.items():
            goal = goals_by_id.get(goal_id)
            if goal is None:
                continue
            caps = {str(t.capability).strip().upper() for t in tasks}
            if (
                "SEARCH_COMMUNITY" in caps
                and "ANALYZE_CONTENT_PATTERNS" in caps
                and "GET_POST_DETAIL" not in caps
            ):
                search_task = next(
                    t for t in tasks if str(t.capability).strip().upper() == "SEARCH_COMMUNITY"
                )
                read_task_id = f"{goal.goal_id}:evidence-read"
                if read_task_id not in used_ids:
                    read_task = TaskNode(
                        task_id=read_task_id,
                        goal_id=goal.goal_id,
                        capability="GET_POST_DETAIL",
                        dependencies=[search_task.task_id],
                    )
                    existing.append(read_task)
                    by_goal.setdefault(goal.goal_id, []).append(read_task)
                    used_ids.add(read_task_id)
                for task in tasks:
                    if str(task.capability).strip().upper() != "ANALYZE_CONTENT_PATTERNS":
                        continue
                    if read_task_id not in task.dependencies:
                        task.dependencies = [*task.dependencies, read_task_id]

        for goal_id, tasks in by_goal.items():
            goal = goals_by_id.get(goal_id)
            if goal is None:
                continue
            generated = [
                task
                for task in tasks
                if str(task.capability).strip().upper() == "GENERATE_CONTENT"
            ]
            if len(generated) != 1 or self._goal_has_explicit_draft_reference(goal):
                continue
            draft_task_id = generated[0].task_id
            for task in tasks:
                if str(task.capability).strip().upper() not in {
                    "SCHEDULE_PUBLISH",
                    "PUBLISH_NOW",
                }:
                    continue
                if self._task_has_explicit_draft_reference(task):
                    continue
                if draft_task_id not in task.dependencies:
                    task.dependencies = [*task.dependencies, draft_task_id]

        # Keep ordinal presentation and artifact-flow derivation aligned with
        # the Goal's semantic capability order even when an LLM supplied its
        # optional TaskNode hints out of order. Dependencies remain the source
        # of execution truth; this only makes the durable plan deterministic.
        return self._order_task_nodes(existing, goal_tree)

    @staticmethod
    def _task_has_explicit_draft_reference(task: TaskNode) -> bool:
        values = task.inputs or {}
        if values.get("draft_id") not in (None, "", []):
            return True
        target = values.get("target")
        return isinstance(target, Mapping) and target.get("draft_id") not in (None, "", [])

    def _goal_has_explicit_draft_reference(self, goal: Goal) -> bool:
        values = self._values_for_goal(goal)
        return values.get("draft_id") not in (None, "", [])

    @staticmethod
    def _order_task_nodes(tasks: list[TaskNode], goal_tree: GoalTree) -> list[TaskNode]:
        """Order TaskNodes by user-visible Goal and each Goal's capability order."""

        ordered: list[TaskNode] = []
        used_ids: set[str] = set()
        by_goal: dict[str, list[TaskNode]] = {}
        for task in tasks:
            by_goal.setdefault(task.goal_id, []).append(task)

        for goal in goal_tree.executable_goals():
            capabilities = [
                str(capability).strip().upper()
                for capability in (
                    goal.required_capabilities
                    or [
                        _GOAL_TYPE_CAPABILITIES.get(
                            str(goal.goal_type).strip().upper(),
                            "GENERATE_CONTENT",
                        )
                    ]
                )
            ]
            goal_tasks = by_goal.get(goal.goal_id, [])
            for capability in capabilities:
                # Extra prerequisite nodes (e.g. the deterministically inserted
                # GET_POST_DETAIL before ANALYZE_CONTENT_PATTERNS) must come
                # before the capability whose tasks depend on them.
                for task in goal_tasks:
                    if task.task_id in used_ids:
                        continue
                    if str(task.capability).strip().upper() in capabilities:
                        continue
                    if any(
                        task.task_id in (other.dependencies or ())
                        for other in goal_tasks
                        if str(other.capability).strip().upper() == capability
                    ):
                        ordered.append(task)
                        used_ids.add(task.task_id)
                for task in goal_tasks:
                    if (
                        task.task_id not in used_ids
                        and str(task.capability).strip().upper() == capability
                    ):
                        ordered.append(task)
                        used_ids.add(task.task_id)
            # Preserve extra planner nodes deterministically after the
            # capability-backed nodes. They can still carry a valid explicit
            # dependency graph and are not discarded by this compatibility
            # adapter.
            for task in goal_tasks:
                if task.task_id not in used_ids:
                    ordered.append(task)
                    used_ids.add(task.task_id)

        # ``GoalTree.validate_tree`` normally makes this tail empty. Keeping
        # it preserves the compiler's existing unknown-goal error path.
        ordered.extend(task for task in tasks if task.task_id not in used_ids)
        return ordered

    @staticmethod
    def _task_dependencies(
        task: TaskNode,
        goal: Goal,
        *,
        task_ids: set[str],
        task_ids_by_goal: dict[str, list[str]],
    ) -> list[str]:
        raw = list(task.dependencies or goal.dependencies)
        result: list[str] = []
        for dependency in raw:
            if dependency in task_ids:
                result.append(dependency)
            else:
                result.extend(task_ids_by_goal.get(dependency, []))
        return list(dict.fromkeys(value for value in result if value != task.task_id))

    def _input_artifact_types(
        self,
        task: TaskNode,
        dependencies: list[str],
        steps: list[PlanStep],
    ) -> list[str]:
        explicit = task.inputs.get("artifact_inputs", task.inputs.get("input_artifact_types", []))
        if isinstance(explicit, str):
            return [explicit]
        if isinstance(explicit, list) and explicit:
            return [str(value) for value in explicit]
        output_by_step = {step.step_id: step.output_artifact_type for step in steps}
        return [
            output_by_step[dependency]
            for dependency in dependencies
            if output_by_step.get(dependency)
        ]

    @staticmethod
    def _constraints(values: list[dict[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for item in values:
            if not isinstance(item, Mapping):
                continue
            key = str(item.get("type", "")).strip().lower()
            if key:
                result[key] = item.get("value", item.get("values", item))
                continue
            for name, value in item.items():
                normalized_name = str(name).strip().lower()
                if normalized_name:
                    result[normalized_name] = value
        return result

    def _capability_for_goal_type(self, goal_type: str) -> str:
        return _GOAL_TYPE_CAPABILITIES.get(
            str(goal_type).strip().upper(),
            "GENERATE_CONTENT",
        )

    def _output_for_capability(self, capability: str) -> str:
        if self._registry is None:
            return ""
        item = self._registry.get(capability)
        return item.output_artifact_type if item is not None else ""

    def _artifact_inputs(self, goal: Goal) -> list[str]:
        values: list[str] = []
        for capability in goal.required_capabilities:
            item = self._registry.get(capability) if self._registry else None
            if item is not None:
                values.extend(item.inputs.required)
        return list(dict.fromkeys(values))


_GOAL_TYPE_CAPABILITIES: dict[str, str] = {
    "SEARCH": "SEARCH_COMMUNITY",
    "CREATE": "GENERATE_CONTENT",
    "VALIDATE": "VALIDATE_QUALITY",
    "MODIFY": "GENERATE_CONTENT",
    "CANCEL": "CANCEL_SCHEDULE",
    "QUERY": "GET_DRAFT",
    "RESEARCH": "SEARCH_COMMUNITY",
    "ANALYZE": "ANALYZE_CONTENT_PATTERNS",
    "PUBLISH": "SCHEDULE_PUBLISH",
}


__all__ = ["GoalCompilationError", "GoalCompiler"]
