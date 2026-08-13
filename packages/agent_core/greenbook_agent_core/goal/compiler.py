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
                    dict(item)
                    for item in goal.constraints
                    if isinstance(item, Mapping)
                ],
                target_hint=(
                    command.target.reference
                    or command.target.explicit_id
                    or command.target.value
                    if command is not None and command.target is not None
                    else None
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
        return TaskPlan(
            task_id=task_id,
            steps=steps,
            plan_source="GOAL_RUNTIME",
            plan_version=max(1, plan_version),
            previous_plan_id=previous_plan_id,
            change_reason=change_reason,
        )

    def to_plan_graph(self, goal_tree: GoalTree, *, command: Command | None = None) -> PlanGraph:
        return self.compile(goal_tree, command=command)

    def to_task_plan(self, goal_tree: GoalTree, *, task_id: str = "") -> TaskPlan:
        return self.compile_plan(goal_tree, task_id=task_id)

    def _constraints_for_goal(
        self,
        goal: Goal,
        *,
        capability: Any | None,
        command: Command | None,
    ) -> dict[str, Any]:
        """Compile structured Goal/Command values into tool input fields.

        Goal descriptions and constraints are already the structured output of
        the intelligence boundary.  This adapter makes the small semantic
        fields required by a Tool contract explicit before the plan crosses
        into Reliable Execution; the Worker still performs the final schema
        validation.
        """

        values = self._constraints(goal.constraints)
        if command is not None:
            for source_name in ("parameters", "entities", "constraints"):
                source = getattr(command, source_name, {}) or {}
                if isinstance(source, Mapping):
                    for key, value in source.items():
                        values.setdefault(str(key).strip().lower(), value)
            if command.references and "references" not in values:
                values["references"] = list(command.references)

        # TargetResolver may emit one structured reference object instead of
        # the older {type, value} constraint form. Normalize only the
        # canonical schedule/draft aliases needed by Tool contracts.
        if "run_at" not in values and values.get("publish_at"):
            values["run_at"] = values["publish_at"]
        if (
            "draft_id" not in values
            and str(values.get("kind", "")).upper() == "DRAFT"
            and values.get("id")
        ):
            values["draft_id"] = values["id"]

        required = list(getattr(getattr(capability, "inputs", None), "required", ()) or ())
        for field in required:
            if field in values and values[field] not in (None, "", []):
                continue
            derived = self._derive_required_value(field, values, goal)
            if derived is not None:
                values[field] = derived

        # A child Goal may intentionally describe only the current operation
        # (for example, "write the first article") while the Command carries
        # the topic, audience, and other user-level constraints. Preserve that
        # semantic context when crossing into Creator; otherwise a valid
        # multi-goal decomposition can lose the subject between planning and
        # content generation. This is context propagation, not a workflow or
        # capability mapping.
        capability_name = str(getattr(capability, "name", "")).upper()
        if capability_name == "GENERATE_CONTENT" and command is not None:
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
            "query": ("query", "keywords", "topic"),
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
            previous = list(by_goal.get(goal.goal_id, []))
            for index, capability in enumerate(missing, start=1):
                task_id = f"{goal.goal_id}:{index}"
                while task_id in used_ids:
                    index += 1
                    task_id = f"{goal.goal_id}:{index}"
                dependencies = list(goal.dependencies)
                if previous:
                    dependencies.append(previous[-1].task_id)
                task = TaskNode(
                    task_id=task_id,
                    goal_id=goal.goal_id,
                    capability=capability,
                    dependencies=list(dict.fromkeys(dependencies)),
                )
                existing.append(task)
                by_goal.setdefault(goal.goal_id, []).append(task)
                previous.append(task)
                used_ids.add(task_id)
        return existing

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
    "MODIFY": "IMPROVE_CONTENT",
    "CANCEL": "CANCEL_SCHEDULE",
    "QUERY": "GET_DRAFT",
    "RESEARCH": "SEARCH_COMMUNITY",
    "ANALYZE": "ANALYZE_CONTENT_PATTERNS",
    "PUBLISH": "SCHEDULE_PUBLISH",
}


__all__ = ["GoalCompilationError", "GoalCompiler"]
