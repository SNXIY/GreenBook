from __future__ import annotations

from dataclasses import dataclass

from app.domain import AgentPlan, CommunityIntent


@dataclass(frozen=True)
class PlanningEvaluation:
    intent_accuracy: float
    task_coverage: float
    planning_efficiency: float
    tool_selection_accuracy: float
    agent_selection_accuracy: float

    @property
    def overall(self) -> float:
        return round(
            (
                self.intent_accuracy
                + self.task_coverage
                + self.planning_efficiency
                + self.tool_selection_accuracy
                + self.agent_selection_accuracy
            )
            / 5,
            4,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "intent_accuracy": self.intent_accuracy,
            "task_coverage": self.task_coverage,
            "planning_efficiency": self.planning_efficiency,
            "tool_selection_accuracy": self.tool_selection_accuracy,
            "agent_selection_accuracy": self.agent_selection_accuracy,
            "overall": self.overall,
        }


def evaluate_plan(
    *,
    intent: CommunityIntent,
    plan: AgentPlan,
    expected_domain: str,
    required_capabilities: set[str],
    required_tools: set[str],
    forbidden_tools: set[str],
    expected_agents: set[str],
) -> PlanningEvaluation:
    planned_capabilities = {
        capability for step in plan.steps for capability in step.capabilities
    }
    planned_tools = [step.tool for step in plan.steps]
    planned_tool_set = set(planned_tools)
    planned_agents = {step.agent for step in plan.steps}

    intent_domain = float(intent.domain == expected_domain)
    intent_capability_recall = _recall(
        required_capabilities, set(intent.required_capabilities)
    )
    intent_accuracy = round((intent_domain + intent_capability_recall) / 2, 4)

    task_coverage = round(
        (
            _recall(required_tools, planned_tool_set)
            + _recall(required_capabilities, planned_capabilities)
        )
        / 2,
        4,
    )
    correct_tools = planned_tool_set & required_tools
    invalid_tools = planned_tool_set & forbidden_tools
    tool_precision = (
        1.0
        if not required_tools and not planned_tool_set
        else len(correct_tools) / max(1, len(planned_tool_set))
    )
    tool_selection_accuracy = round(
        max(
            0.0,
            tool_precision
            - len(invalid_tools) / max(1, len(forbidden_tools)),
        ),
        4,
    )
    duplicate_penalty = max(0, len(planned_tools) - len(planned_tool_set))
    excess = max(0, len(planned_tools) - len(required_tools))
    planning_efficiency = round(
        max(0.0, 1 - (duplicate_penalty + excess * 0.25) / max(1, len(planned_tools))),
        4,
    )
    agent_selection_accuracy = round(
        _recall(expected_agents, planned_agents),
        4,
    )
    return PlanningEvaluation(
        intent_accuracy=intent_accuracy,
        task_coverage=task_coverage,
        planning_efficiency=planning_efficiency,
        tool_selection_accuracy=tool_selection_accuracy,
        agent_selection_accuracy=agent_selection_accuracy,
    )


def _recall(expected: set[str], actual: set[str]) -> float:
    if not expected:
        return 1.0
    return len(expected & actual) / len(expected)
