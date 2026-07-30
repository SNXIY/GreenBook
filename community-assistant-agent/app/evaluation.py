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


@dataclass(frozen=True)
class RuntimeEvaluation:
    task_recovery_rate: float
    stale_result_rejection_rate: float
    approval_accuracy: float
    artifact_version_correctness: float
    tool_job_completion_rate: float

    @property
    def overall(self) -> float:
        return round(
            (
                self.task_recovery_rate
                + self.stale_result_rejection_rate
                + self.approval_accuracy
                + self.artifact_version_correctness
                + self.tool_job_completion_rate
            )
            / 5,
            4,
        )

    def as_dict(self) -> dict[str, float]:
        return {
            "task_recovery_rate": self.task_recovery_rate,
            "stale_result_rejection_rate": self.stale_result_rejection_rate,
            "approval_accuracy": self.approval_accuracy,
            "artifact_version_correctness": self.artifact_version_correctness,
            "tool_job_completion_rate": self.tool_job_completion_rate,
            "overall": self.overall,
        }


@dataclass(frozen=True)
class RetrievalEvaluation:
    hit_rate: float
    mrr: float

    def as_dict(self) -> dict[str, float]:
        return {"hit_rate": self.hit_rate, "mrr": self.mrr}


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


def evaluate_runtime(
    *,
    resumed_tasks: int,
    recovered_tasks: int,
    stale_results: int,
    rejected_stale_results: int,
    approval_decisions: int,
    correct_approval_decisions: int,
    artifact_versions: int,
    correct_artifact_versions: int,
    terminal_tool_jobs: int,
    completed_tool_jobs: int,
) -> RuntimeEvaluation:
    return RuntimeEvaluation(
        task_recovery_rate=_ratio(recovered_tasks, resumed_tasks),
        stale_result_rejection_rate=_ratio(
            rejected_stale_results, stale_results
        ),
        approval_accuracy=_ratio(
            correct_approval_decisions, approval_decisions
        ),
        artifact_version_correctness=_ratio(
            correct_artifact_versions, artifact_versions
        ),
        tool_job_completion_rate=_ratio(
            completed_tool_jobs, terminal_tool_jobs
        ),
    )


def evaluate_retrieval(
    *,
    relevant_by_query: list[set[str]],
    ranked_results: list[list[str]],
) -> RetrievalEvaluation:
    if len(relevant_by_query) != len(ranked_results):
        raise ValueError("Retrieval labels and rankings must have equal length")
    if not relevant_by_query:
        return RetrievalEvaluation(hit_rate=1.0, mrr=1.0)
    hits = 0
    reciprocal_ranks = 0.0
    for relevant, ranking in zip(relevant_by_query, ranked_results):
        first_rank = next(
            (
                index
                for index, item_id in enumerate(ranking, start=1)
                if item_id in relevant
            ),
            None,
        )
        if first_rank is not None:
            hits += 1
            reciprocal_ranks += 1 / first_rank
    return RetrievalEvaluation(
        hit_rate=round(hits / len(relevant_by_query), 4),
        mrr=round(reciprocal_ranks / len(relevant_by_query), 4),
    )


def _recall(expected: set[str], actual: set[str]) -> float:
    if not expected:
        return 1.0
    return len(expected & actual) / len(expected)


def _ratio(numerator: int, denominator: int) -> float:
    if min(numerator, denominator) < 0:
        raise ValueError("Evaluation counts cannot be negative")
    if numerator > denominator:
        raise ValueError("Successful count cannot exceed total count")
    return 1.0 if denominator == 0 else round(numerator / denominator, 4)
