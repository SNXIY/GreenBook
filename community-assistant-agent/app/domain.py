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


class TargetBinding(ApiModel):
    """A concrete object selected for the current conversation goal.

    Workspace entities are candidates; a TargetBinding is the control-plane
    reference that a write-capable tool is allowed to consume.
    """

    target_type: Literal["DRAFT", "POST", "SCHEDULE", "ARTIFACT"]
    role: Literal["CONTENT", "SCHEDULE", "PUBLICATION", "INTERACTION"] = "CONTENT"
    target_id: str = Field(min_length=1, max_length=128)
    artifact_id: str | None = Field(default=None, max_length=128)
    content_sha256: str | None = Field(default=None, max_length=64)
    version: int = Field(default=1, ge=1)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    resolution_method: Literal[
        "ACTIVE_TARGET",
        "EXPLICIT_ID",
        "EXPLICIT_TITLE",
        "LINEAGE",
        "SEMANTIC_MATCH",
        "USER_SELECTION",
        "TOOL_OUTPUT",
    ] = "ACTIVE_TARGET"
    schedule_id: str | None = Field(default=None, max_length=128)
    content_artifact_id: str | None = Field(default=None, max_length=128)
    content_artifact_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def infer_role_from_legacy_target_type(self) -> "TargetBinding":
        """Keep pre-role bindings readable while making role explicit."""
        if self.role == "CONTENT" and self.target_type == "SCHEDULE":
            self.role = "SCHEDULE"
        elif self.role == "CONTENT" and self.target_type == "ARTIFACT":
            self.role = "INTERACTION"
        return self


class ResolvedTargetView(ApiModel):
    """An operation-scoped, read-only projection of a Goal target."""

    goal_id: str
    role: Literal["CONTENT", "SCHEDULE", "PUBLICATION", "INTERACTION"]
    target_type: Literal["DRAFT", "POST", "SCHEDULE", "ARTIFACT"]
    target_id: str = Field(min_length=1, max_length=128)
    artifact_id: str | None = Field(default=None, max_length=128)
    content_sha256: str | None = Field(default=None, max_length=64)
    schedule_id: str | None = Field(default=None, max_length=128)
    content_artifact_id: str | None = Field(default=None, max_length=128)
    content_artifact_version: int | None = Field(default=None, ge=1)
    resolution_method: Literal["GOAL_TARGET_CONTEXT"] = "GOAL_TARGET_CONTEXT"

    @classmethod
    def from_binding(
        cls,
        *,
        goal_id: str,
        binding: TargetBinding,
    ) -> "ResolvedTargetView":
        return cls(
            goal_id=goal_id,
            role=binding.role,
            target_type=binding.target_type,
            target_id=binding.target_id,
            artifact_id=binding.artifact_id,
            content_sha256=binding.content_sha256,
            schedule_id=binding.schedule_id,
            content_artifact_id=binding.content_artifact_id,
            content_artifact_version=binding.content_artifact_version,
        )


class TargetCandidate(ApiModel):
    """An authorized object candidate considered for the current turn."""

    target_id: str = Field(min_length=1, max_length=128)
    artifact_id: str | None = Field(default=None, max_length=128)
    type: Literal["DRAFT", "POST", "SCHEDULE", "ARTIFACT"]
    label: str | None = Field(default=None, max_length=240)
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=500)
    content_artifact_id: str | None = Field(default=None, max_length=128)
    content_artifact_version: int | None = Field(default=None, ge=1)


class TargetContext(ApiModel):
    """Typed conversation focus separated by side-effect domain.

    A draft is not a schedule. Keeping these bindings in separate slots
    prevents a content target from being proposed for schedule mutations.
    ``active_target`` remains on older records for backward compatibility, but
    resolution must use this context first.
    """

    content_target: TargetBinding | None = None
    schedule_target: TargetBinding | None = None
    publication_target: TargetBinding | None = None
    interaction_target: TargetBinding | None = None

    def for_operation(self, operation: str) -> TargetBinding | None:
        if operation in {"OPEN_PLAN", "CREATE_POST"}:
            return None
        if operation in {"UPDATE_SCHEDULE", "CANCEL_SCHEDULE", "QUERY_SCHEDULE"}:
            return self.schedule_target
        if operation in {
            "APPEND_CONTENT",
            "REPLACE_CONTENT",
            "UPDATE_TITLE",
            "QUERY_CONTENT",
        }:
            return self.content_target
        if operation == "QUERY_PUBLICATION_STATUS":
            return (
                self.publication_target
                or self.schedule_target
                or self.content_target
            )
        if operation == "PUBLISH_NOW":
            return self.content_target or self.publication_target or self.schedule_target
        return self.content_target or self.interaction_target


class PendingClarification(ApiModel):
    """A user decision required before a side-effecting plan may run."""

    kind: Literal["TARGET", "TEMPORAL_SCHEDULE"] = "TARGET"
    question: str = Field(min_length=1, max_length=1_000)
    candidates: list[TargetCandidate] = Field(default_factory=list, max_length=8)
    delta_id: str | None = None
    goal_id: str | None = Field(default=None, max_length=64)
    original_message: str | None = Field(default=None, max_length=10_000)
    temporal: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _target_requires_candidates(self) -> PendingClarification:
        if self.kind == "TARGET" and not self.candidates:
            raise ValueError("TARGET clarification requires at least one candidate")
        return self


class TurnIntent(ApiModel):
    """Goal-agnostic interpretation of one user turn."""

    operation: Literal[
        "CREATE_POST",
        "APPEND_CONTENT",
        "REPLACE_CONTENT",
        "UPDATE_TITLE",
        "UPDATE_SCHEDULE",
        "PUBLISH_NOW",
        "CANCEL_SCHEDULE",
        "QUERY_SCHEDULE",
        "QUERY_CONTENT",
        "QUERY_PUBLICATION_STATUS",
        "OPEN_PLAN",
        "REPLY_COMMENT",
        "CONTINUE_ANALYSIS",
    ]
    operation_class: Literal["READ", "WRITE", "SIDE_EFFECT"]
    target_role: Literal["CONTENT", "SCHEDULE", "PUBLICATION", "INTERACTION"] | None = None
    semantic_subject: str = Field(default="", max_length=500)
    raw_message: str = Field(default="", max_length=10_000)
    explicit_refs: list[str] = Field(default_factory=list, max_length=12)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class GoalMatch(ApiModel):
    goal_id: str
    label: str
    score: float = Field(ge=0.0, le=1.0)
    resolution_method: Literal[
        "EXPLICIT_ID",
        "ARTIFACT_TITLE",
        "ARTIFACT_TITLE_EXACT",
        "GOAL_SUMMARY",
        "GOAL_SUMMARY_EXACT",
        "SEMANTIC_SIMILARITY",
        "SOLE_GOAL",
        "RECENT_ACTIVE",
        "WORD_MATCH",
    ]


class GoalResolution(ApiModel):
    outcome: Literal[
        "RESOLVED",
        "NEW_GOAL",
        "NEEDS_CLARIFICATION",
        "NOT_FOUND",
    ]
    goal_id: str | None = None
    candidates: list[GoalMatch] = Field(default_factory=list, max_length=8)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ConversationGoal(ApiModel):
    """Durable business goal shared by multiple Runs in one conversation."""

    goal_id: str
    conversation_id: str
    intent: str = Field(min_length=1, max_length=64)
    summary: str | None = Field(default=None, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    artifact_titles: list[str] = Field(default_factory=list, max_length=20)
    artifact_topics: list[str] = Field(default_factory=list, max_length=30)
    explicit_refs: list[str] = Field(default_factory=list, max_length=40)
    status: Literal[
        "ACTIVE",
        "WAITING_CLARIFICATION",
        "WAITING_APPROVAL",
        "PAUSED",
        "COMPLETED",
        "CANCELLED",
        "FAILED",
    ] = "ACTIVE"
    phase: str = Field(default="DISCOVERING", min_length=1, max_length=32)
    active_target_ref: str | None = Field(default=None, max_length=160)
    # Deprecated compatibility projection. Business decisions must use
    # target_context, never this field.
    active_target: TargetBinding | None = None
    target_context: TargetContext = Field(default_factory=TargetContext)
    pending_clarification: PendingClarification | None = None
    pending_delta_id: str | None = None
    version: int = Field(default=1, ge=1)
    updated_at: datetime | None = None


class IntentDelta(ApiModel):
    """A turn-level change applied to an existing ConversationGoal."""

    delta_id: str
    goal_id: str
    run_id: str
    message_id: str
    operation: Literal[
        "CREATE_POST",
        "APPEND_CONTENT",
        "REPLACE_CONTENT",
        "UPDATE_TITLE",
        "UPDATE_SCHEDULE",
        "PUBLISH_NOW",
        "CANCEL_SCHEDULE",
        "QUERY_SCHEDULE",
        "QUERY_CONTENT",
        "QUERY_PUBLICATION_STATUS",
        "OPEN_PLAN",
        "REPLY_COMMENT",
        "CONTINUE_ANALYSIS",
    ]
    operation_class: Literal["READ", "WRITE", "SIDE_EFFECT"] = "WRITE"
    target_role: Literal["CONTENT", "SCHEDULE", "PUBLICATION", "INTERACTION"] | None = None
    target_ref: str | None = Field(default=None, max_length=160)
    delta: dict[str, Any] = Field(default_factory=dict)
    preserve: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    status: Literal["ACTIVE", "APPLIED", "REJECTED", "FAILED", "SUPERSEDED"] = "ACTIVE"

    @model_validator(mode="before")
    @classmethod
    def infer_operation_class(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("operation_class") is not None:
            return value
        operation = str(value.get("operation") or "")
        if operation.startswith("QUERY_"):
            operation_class = "READ"
        elif operation in {"UPDATE_SCHEDULE", "PUBLISH_NOW", "CANCEL_SCHEDULE"}:
            operation_class = "SIDE_EFFECT"
        elif operation == "OPEN_PLAN":
            operation_class = "WRITE"
        else:
            operation_class = "WRITE"
        return {**value, "operation_class": operation_class}


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
    parent_artifact_id: str | None = None
    version: int
    change_type: str | None = None
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
    pending_clarification: dict[str, Any] | None = None
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
    primary_capability: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]{1,63}$",
    )
    capabilities: list[str] = Field(default_factory=list, max_length=20)
    tool: str = Field(min_length=3, max_length=80)
    label: str = Field(min_length=1, max_length=120)
    success_criteria: list[str] = Field(default_factory=list, max_length=10)
    expected_artifact_type: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_.-]{1,63}$",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list, max_length=20)
    artifact_sources: dict[str, list[str]] = Field(default_factory=dict)
    condition: TaskCondition | None = None
    max_attempts: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def normalize_capability_contract(self) -> "AgentPlanStep":
        normalized = list(dict.fromkeys(self.capabilities))
        if self.primary_capability is None and normalized:
            self.primary_capability = normalized[0]
        if (
            self.primary_capability is not None
            and self.primary_capability not in normalized
        ):
            normalized.insert(0, self.primary_capability)
        self.capabilities = normalized
        return self


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
    turn_relation: Literal[
        "NEW_GOAL",
        "CONTINUE",
        "MODIFY",
        "CANCEL",
        "RETRY",
        "QUERY_STATE",
    ] = "NEW_GOAL"
    referenced_entities: list[str] = Field(default_factory=list, max_length=8)
    direct_response: str | None = Field(default=None, max_length=10_000)
    plan: AgentPlan | None = None
    primary_operation: str | None = Field(default=None, max_length=64)
    open_plan: bool | None = None
    follow_up_prompts: list[str] = Field(default_factory=list, max_length=3)

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


class AdaptiveRoutingDecision(ApiModel):
    """Lean semantic output produced by the Adaptive Router model.

    Executable AgentPlan objects are compiled by deterministic code after this
    boundary. Keeping the model contract shallow prevents a simple route choice
    from failing because one nested plan field was omitted or misspelled.
    """

    execution_path: Literal["DIRECT", "TOOL", "CREATOR", "ORCHESTRATED"]
    classification_summary: str = Field(min_length=1, max_length=240)
    intent: CommunityIntent
    turn_relation: Literal[
        "NEW_GOAL",
        "CONTINUE",
        "MODIFY",
        "CANCEL",
        "RETRY",
        "QUERY_STATE",
    ] = "NEW_GOAL"
    referenced_entities: list[str] = Field(default_factory=list, max_length=8)
    direct_response: str | None = Field(default=None, max_length=10_000)
    tool: str | None = Field(default=None, min_length=3, max_length=80)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Optional TurnPlan hints — validated by the control plane; never trusted
    # for authorization or tool selection alone.
    primary_operation: str | None = Field(default=None, max_length=64)
    open_plan: bool | None = None
    follow_up_prompts: list[str] = Field(default_factory=list, max_length=3)


class VerificationDecision(ApiModel):
    decision: Literal["COMPLETE", "REPLAN", "FAILED"]
    reason: str = Field(min_length=1, max_length=500)
    next_focus: str = Field(default="", max_length=500)


class ProgressDecision(ApiModel):
    decision: Literal["CONTINUE", "REPLAN", "FAILED"]
    progress_made: bool
    in_loop: bool = False
    reason: str = Field(min_length=1, max_length=500)
    next_focus: str = Field(default="", max_length=500)


class PlanDiagnostic(ApiModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,79}$")
    message: str = Field(min_length=1, max_length=1_000)
    task_id: str | None = Field(default=None, max_length=80)
    details: dict[str, Any] = Field(default_factory=dict)


class PlanCompileResult(ApiModel):
    status: Literal["EXECUTABLE", "NEEDS_REPLAN", "NEEDS_INPUT", "UNSUPPORTED"]
    diagnostics: list[PlanDiagnostic] = Field(default_factory=list, max_length=50)
    compiled_plan: AgentPlan | None = None


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
