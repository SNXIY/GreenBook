"""Pydantic models for semantic Goals and executable task nodes.

Goals describe what the user wants.  TaskNodes are the smallest capability
requirements emitted for the planner.  Neither model contains MCP tool names,
queue state, or execution state.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator


class Goal(BaseModel):
    """One semantic goal in a potentially nested goal tree."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    goal_id: str = Field(min_length=1)
    description: str = ""
    goal_type: str = "TASK"
    parent_goal: str | None = None
    children: list[Goal] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    # These fields keep business semantics attached to the logical Goal
    # instead of leaving operation/time/publication intent in the command's
    # request-wide scalar fields.  ``constraints`` remains supported for
    # backwards-compatible GoalTree payloads; the compiler normalizes both
    # representations before creating a PlanStep.
    semantic_operation: str = ""
    target: dict[str, Any] = Field(default_factory=dict)
    temporal_constraint: dict[str, Any] = Field(default_factory=dict)
    publication_intent: str = ""
    expected_outputs: list[str] = Field(default_factory=list)


class TaskNode(BaseModel):
    """A capability requirement that can be compiled into an existing plan."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    task_id: str = Field(min_length=1)
    goal_id: str = Field(min_length=1)
    capability: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    status: str = "PENDING"


class GoalTree(BaseModel):
    """Structured output of GoalDecomposer.

    ``root`` is the preferred representation.  ``goals`` is retained as a
    small interchange convenience for callers that already hold a flat list;
    when root is omitted, the first flat goal becomes the root.
    """

    model_config = ConfigDict(extra="forbid")

    root: Goal | None = Field(
        default=None,
        validation_alias=AliasChoices("root", "root_goal"),
    )
    goals: list[Goal] = Field(default_factory=list)
    task_nodes: list[TaskNode] = Field(default_factory=list)
    command_id: str = ""
    source: str = "LLM_STRUCTURED_OUTPUT"
    version: int = Field(default=1, ge=1)

    @model_validator(mode="before")
    @classmethod
    def _normalize_flat_child_references(cls, value: Any) -> Any:
        """Resolve the equivalent flat ``goals`` wire representation.

        Some OpenAI-compatible providers emit both a nested ``root`` and a
        flat ``goals`` list. In the flat copy, ``children`` may be goal IDs
        instead of embedded Goal objects. The canonical model remains nested;
        this adapter only resolves references already present in the payload.
        It never creates a goal or infers a dependency.
        """

        if not isinstance(value, dict):
            return value
        payload = dict(value)
        raw_root = payload.get("root", payload.get("root_goal"))
        raw_goals = payload.get("goals") or []
        candidates = [
            item
            for item in [raw_root, *raw_goals]
            if isinstance(item, dict) and item.get("goal_id")
        ]
        by_id = {str(item["goal_id"]): item for item in candidates}
        if not by_id:
            return payload

        def resolve(item: Any, stack: tuple[str, ...] = ()) -> Any:
            if not isinstance(item, dict):
                return item
            goal_id = str(item.get("goal_id") or "")
            if goal_id and goal_id in stack:
                raise ValueError(f"GoalTree contains a child cycle at '{goal_id}'")
            current = dict(item)
            children = current.get("children") or []
            normalized: list[Any] = []
            for child in children:
                child_item = by_id.get(str(child)) if isinstance(child, str) else child
                normalized.append(
                    resolve(child_item, (*stack, goal_id))
                    if isinstance(child_item, dict)
                    else child_item
                )
            if "children" in current:
                current["children"] = normalized
            return current

        if isinstance(raw_root, dict):
            root_key = "root" if "root" in payload else "root_goal"
            payload[root_key] = resolve(raw_root)
        if isinstance(raw_goals, list):
            payload["goals"] = [resolve(item) for item in raw_goals]
        return payload

    @model_validator(mode="after")
    def _require_root(self) -> GoalTree:
        if self.root is None and self.goals:
            self.root = self.goals[0]
        if self.root is None:
            raise ValueError("GoalTree requires a root Goal")
        # Some structured-output providers use the literal label ``root``
        # for the parent reference while assigning the actual root a stable
        # semantic id.  Normalize that wire alias only when the root is
        # unambiguous; all other unknown parent references remain invalid.
        root_id = self.root.goal_id
        if root_id != "root":
            for goal in self.all_goals():
                if goal.parent_goal == "root":
                    goal.parent_goal = root_id
        return self

    def all_goals(self) -> list[Goal]:
        """Return nested goals in stable depth-first order, without duplicates."""

        result: list[Goal] = []
        seen: set[str] = set()

        def visit(goal: Goal) -> None:
            if goal.goal_id in seen:
                return
            seen.add(goal.goal_id)
            result.append(goal)
            for child in goal.children:
                visit(child)

        visit(self.root)  # type: ignore[arg-type]
        for goal in self.goals:
            visit(goal)
        return result

    def executable_goals(self) -> list[Goal]:
        """Return leaf goals, or the root when the tree has one goal."""

        goals = self.all_goals()
        leaves = [goal for goal in goals if not goal.children]
        return leaves or goals[:1]

    def validate_tree(self) -> GoalTree:
        """Validate references and cycles before graph compilation."""

        goals: list[Goal] = []
        visiting: set[str] = set()
        seen: set[str] = set()

        def visit(goal: Goal) -> None:
            if goal.goal_id in visiting:
                raise ValueError("GoalTree contains a child cycle")
            if goal.goal_id in seen:
                raise ValueError(
                    f"GoalTree contains duplicate goal_id '{goal.goal_id}'"
                )
            visiting.add(goal.goal_id)
            seen.add(goal.goal_id)
            goals.append(goal)
            for child in goal.children:
                visit(child)
            visiting.remove(goal.goal_id)

        visit(self.root_goal)
        for goal in self.goals:
            if goal.goal_id not in seen:
                visit(goal)
        goal_ids = [goal.goal_id for goal in goals]
        known = set(goal_ids)
        for goal in goals:
            if goal.parent_goal and goal.parent_goal not in known:
                raise ValueError(
                    f"Goal '{goal.goal_id}' references unknown parent_goal "
                    f"'{goal.parent_goal}'"
                )
            unknown_dependencies = set(goal.dependencies) - known
            if unknown_dependencies:
                raise ValueError(
                    f"Goal '{goal.goal_id}' references unknown dependencies: "
                    f"{sorted(unknown_dependencies)}"
                )
        task_goal_ids = {task.goal_id for task in self.task_nodes}
        unknown_task_goals = task_goal_ids - known
        if unknown_task_goals:
            raise ValueError(
                "TaskNode references unknown goals: "
                f"{sorted(unknown_task_goals)}"
            )
        task_ids = [task.task_id for task in self.task_nodes]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("GoalTree contains duplicate task_id values")
        return self

    @property
    def root_goal(self) -> Goal:
        """Compatibility spelling for callers that prefer an explicit name."""

        return self.root  # type: ignore[return-value]


__all__ = ["Goal", "GoalTree", "TaskNode"]
