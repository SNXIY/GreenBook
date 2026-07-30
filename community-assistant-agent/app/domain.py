from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Principal(ApiModel):
    user_id: str
    tenant_id: str
    role: str
    display_name: str
    token: str = Field(exclude=True)


class ConversationCreate(ApiModel):
    title: str | None = Field(default=None, max_length=120)
    context_post_id: str | None = Field(default=None, max_length=64)
    surface: Literal["HOME", "COMMENT", "POST"] = "HOME"


class ConversationView(ApiModel):
    conversation_id: str
    title: str
    context_post_id: str | None
    surface: str
    updated_at: datetime


class MessageCreate(ApiModel):
    content: str = Field(min_length=1, max_length=10_000)
    context_post_id: str | None = Field(default=None, max_length=64)
    context_comment_id: str | None = Field(default=None, max_length=64)
    client_timezone: str = Field(default="Asia/Shanghai", max_length=64)


class MessageView(ApiModel):
    message_id: str
    role: str
    content: str
    parts: list[dict[str, Any]]
    run_id: str | None
    created_at: datetime


class RunAccepted(ApiModel):
    run_id: str
    conversation_id: str
    status: str
    events_url: str
    replayed: bool = False


class StepView(ApiModel):
    step_id: str
    ordinal: int
    kind: str
    tool_name: str | None
    label: str
    status: str
    output: dict[str, Any] | None
    error: str | None
    started_at: datetime | None
    completed_at: datetime | None
    task_key: str | None = None
    agent_name: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 1


class ArtifactView(ApiModel):
    artifact_id: str
    run_id: str
    step_id: str | None
    task_id: str
    agent: str
    artifact_type: str
    parent_artifact_ids: list[str]
    version: int
    content: dict[str, Any]
    content_hash: str
    created_at: datetime


class ToolJobView(ApiModel):
    job_id: str
    run_id: str
    step_ordinal: int
    tool_name: str
    status: str
    attempts: int
    max_attempts: int
    next_attempt_at: datetime | None
    result: dict[str, Any] | None
    error: str | None
    dead_lettered_at: datetime | None
    created_at: datetime
    updated_at: datetime


class PolicyAuditView(ApiModel):
    audit_id: str
    run_id: str
    action: str
    resource: dict[str, Any]
    decision: str
    reason: str
    policy_version: str
    created_at: datetime


class RunView(ApiModel):
    run_id: str
    conversation_id: str
    goal: str
    status: str
    execution_path: str
    workload_lane: str
    intent: str | None
    summary: str | None
    final_response: str | None
    error: str | None
    trace_id: str
    budget: dict[str, int]
    timing: dict[str, int | None]
    intent_detail: dict[str, Any] | None = None
    task_ledger: dict[str, Any] = Field(default_factory=dict)
    progress_ledger: dict[str, Any] = Field(default_factory=dict)
    approval: "ApprovalView | None" = None
    steps: list[StepView]
    created_at: datetime
    updated_at: datetime


class RunListStepView(ApiModel):
    step_id: str
    label: str
    status: str


class RunListItemView(ApiModel):
    run_id: str
    conversation_id: str
    goal: str
    status: str
    intent: str | None
    summary: str | None
    error: str | None
    trace_id: str
    approval: "ApprovalView | None" = None
    steps: list[RunListStepView]
    creator_task_ids: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ScheduledActionView(ApiModel):
    action_id: str
    run_id: str
    draft_id: str
    instruction: str
    run_at: datetime
    status: str
    attempts: int
    result: dict[str, Any] | None
    error: str | None


class ScheduledActionAttemptView(ApiModel):
    attempt: int
    status: str
    worker_id: str
    result: dict[str, Any] | None
    error: str | None
    started_at: datetime
    completed_at: datetime | None


class CommunityIntent(ApiModel):
    # Intent labels are catalog-driven instead of a closed Python enum. New
    # community abilities can extend the catalog without changing this schema.
    domain: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    goal: str = Field(min_length=1, max_length=500)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    constraints: list[str] = Field(default_factory=list, max_length=20)
    required_capabilities: list[str] = Field(default_factory=list, max_length=20)
    entities: dict[str, str] = Field(default_factory=dict)
    scope: dict[str, Any] = Field(default_factory=dict)
    risk: Literal["low", "medium", "high", "critical"] = "low"
    confidence: float = Field(ge=0.0, le=1.0)


class TaskCondition(ApiModel):
    source_task: str = Field(min_length=1, max_length=80)
    path: str = Field(min_length=1, max_length=160)
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "exists"]
    value: Any = None
    on_false: Literal["skip", "fail"] = "skip"


class AgentPlanStep(ApiModel):
    task_id: str | None = Field(default=None, min_length=1, max_length=80)
    agent: str = Field(default="AutoRouter", min_length=2, max_length=80)
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    tool: str = Field(min_length=3, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    condition: TaskCondition | None = None
    max_attempts: int = Field(default=2, ge=1, le=5)


class AgentPlan(ApiModel):
    intent: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    summary: str = Field(min_length=1, max_length=240)
    response_guidance: str = Field(default="", max_length=1_000)
    intent_detail: CommunityIntent | None = None
    steps: list[AgentPlanStep] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def validate_dag(self) -> "AgentPlan":
        legacy = all(step.task_id is None for step in self.steps)
        for index, step in enumerate(self.steps, start=1):
            if step.task_id is None:
                step.task_id = f"task-{index}"
            if legacy and index > 1 and not step.depends_on:
                step.depends_on = [f"task-{index - 1}"]

        task_ids = [str(step.task_id) for step in self.steps]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("Task DAG contains duplicate task_id")
        known = set(task_ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise ValueError(f"Task {step.task_id} has unknown dependencies: {unknown}")
            if step.task_id in step.depends_on:
                raise ValueError(f"Task {step.task_id} cannot depend on itself")
            if step.condition and step.condition.source_task not in step.depends_on:
                raise ValueError("Conditional source_task must also be a dependency")

        visiting: set[str] = set()
        visited: set[str] = set()
        dependencies = {str(step.task_id): set(step.depends_on) for step in self.steps}

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise ValueError("Task DAG contains a cycle")
            visiting.add(task_id)
            for dependency in dependencies[task_id]:
                visit(dependency)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in task_ids:
            visit(task_id)
        return self

    def execution_layers(self) -> list[list[AgentPlanStep]]:
        remaining = {str(step.task_id): step for step in self.steps}
        completed: set[str] = set()
        layers: list[list[AgentPlanStep]] = []
        while remaining:
            ready = [
                step
                for step in remaining.values()
                if set(step.depends_on).issubset(completed)
            ]
            if not ready:
                raise ValueError("Task DAG has no executable frontier")
            ready.sort(key=lambda step: self.steps.index(step))
            layers.append(ready)
            for step in ready:
                task_id = str(step.task_id)
                completed.add(task_id)
                del remaining[task_id]
        return layers


class AdaptiveExecutionDecision(ApiModel):
    execution_path: Literal["DIRECT", "TOOL", "CREATOR", "ORCHESTRATED"]
    classification_summary: str = Field(min_length=1, max_length=240)
    intent: CommunityIntent
    direct_response: str | None = Field(default=None, max_length=10_000)
    plan: AgentPlan | None = None

    @model_validator(mode="after")
    def validate_execution_contract(self) -> "AdaptiveExecutionDecision":
        if self.execution_path == "DIRECT":
            if not self.direct_response or not self.direct_response.strip():
                raise ValueError("DIRECT execution requires direct_response")
            if self.plan is not None and self.plan.steps:
                raise ValueError("DIRECT execution cannot contain tool steps")
        elif self.execution_path in {"TOOL", "CREATOR"}:
            if self.plan is None or not self.plan.steps:
                raise ValueError(
                    f"{self.execution_path} execution requires an executable plan"
                )
            if self.direct_response is not None:
                raise ValueError(
                    f"{self.execution_path} execution cannot precompute a response"
                )
        elif self.direct_response is not None:
            raise ValueError(
                "ORCHESTRATED execution cannot precompute a final response"
            )
        return self


class VerificationDecision(ApiModel):
    decision: Literal["COMPLETE", "REPLAN", "FAILED"]
    reason: str = Field(min_length=1, max_length=500)
    next_focus: str = Field(default="", max_length=500)


class ApprovalView(ApiModel):
    approval_id: str
    action: str
    status: str
    description: str
    preview: dict[str, Any]
    expires_at: datetime
    expected_run_version: int


class ApprovalDecision(ApiModel):
    decision: Literal["APPROVE", "REJECT"]
    expected_run_version: int = Field(ge=1)


class MemoryCreate(ApiModel):
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=1_000)


class MemoryView(ApiModel):
    memory_id: str
    key: str
    value: str
    created_at: datetime
    updated_at: datetime


class MemoryProfileUpdate(ApiModel):
    episodic_enabled: bool
    semantic_enabled: bool


class MemoryProfileView(ApiModel):
    episodic_enabled: bool
    semantic_enabled: bool
    retention_days: int
    semantic_backend: str
    embedding_provider: str


class EpisodicMemoryView(ApiModel):
    episode_id: str
    run_id: str
    intent: str | None
    goal: str
    summary: str
    outcome: str
    tool_names: list[str]
    artifact_refs: list[dict[str, str]]
    importance: float
    occurred_at: datetime
    expires_at: datetime
    recall_count: int
