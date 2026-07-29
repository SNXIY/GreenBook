from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorGoal,
    CreatorTaskKind,
    RuntimeStartRequest,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RuntimeModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ArtifactKind(str, Enum):
    SOURCE_DRAFT = "SOURCE_DRAFT"
    CREATOR_PROFILE = "CREATOR_PROFILE"
    CONTENT_ANALYSIS = "CONTENT_ANALYSIS"
    EVIDENCE_PACK = "EVIDENCE_PACK"
    TOPIC_OPTIONS = "TOPIC_OPTIONS"
    CONTENT_OUTLINE = "CONTENT_OUTLINE"
    DRAFT = "DRAFT"
    CRITIQUE = "CRITIQUE"
    EVALUATION_REPORT = "EVALUATION_REPORT"
    DECISION_REQUEST = "DECISION_REQUEST"
    HUMAN_DECISION = "HUMAN_DECISION"
    FINAL_CONTENT = "FINAL_CONTENT"


class AgentCapability(str, Enum):
    LOAD_CREATOR_MEMORY = "LOAD_CREATOR_MEMORY"
    ANALYZE_CONTENT = "ANALYZE_CONTENT"
    RESEARCH_TOPIC = "RESEARCH_TOPIC"
    PLAN_TOPICS = "PLAN_TOPICS"
    BUILD_OUTLINE = "BUILD_OUTLINE"
    WRITE_DRAFT = "WRITE_DRAFT"
    REVISE_DRAFT = "REVISE_DRAFT"
    CRITIQUE_CONTENT = "CRITIQUE_CONTENT"
    EVALUATE_RUN = "EVALUATE_RUN"


class PlanStepStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class SupervisorAction(str, Enum):
    DISPATCH = "DISPATCH"
    REQUEST_HUMAN = "REQUEST_HUMAN"
    FINISH = "FINISH"
    FAIL = "FAIL"


class RuntimeControlStatus(str, Enum):
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class RunIdentity(RuntimeModel):
    task_id: str
    run_id: str
    thread_id: str
    tenant_id: str
    creator_id: str
    session_id: str | None = None
    task_kind: CreatorTaskKind
    trace_id: str
    execution_attempt: int = Field(ge=1)

    @classmethod
    def from_request(cls, request: RuntimeStartRequest) -> "RunIdentity":
        return cls(
            task_id=request.task_id,
            run_id=request.run_id,
            thread_id=request.thread_id,
            tenant_id=request.tenant_id,
            creator_id=request.creator_id,
            session_id=request.session_id,
            task_kind=request.kind,
            trace_id=request.trace_id,
            execution_attempt=request.execution_attempt,
        )


class ArtifactPayload(RuntimeModel):
    kind: ArtifactKind
    content: dict[str, Any]
    parent_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class CreatorArtifact(RuntimeModel):
    id: str = Field(min_length=1, max_length=128)
    tenant_id: str
    creator_id: str
    task_id: str
    run_id: str
    step_id: str
    kind: ArtifactKind
    producer: str
    revision: int = Field(ge=1)
    content: dict[str, Any]
    parent_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    content_sha256: str = Field(min_length=64, max_length=64)
    created_at: datetime

    def as_ref(self) -> "ArtifactRef":
        return ArtifactRef(
            id=self.id,
            kind=self.kind,
            producer=self.producer,
            step_id=self.step_id,
            revision=self.revision,
            parent_ids=self.parent_ids,
            metadata=self.metadata,
            confidence=self.confidence,
            content_sha256=self.content_sha256,
            created_at=self.created_at,
        )


class ArtifactRef(RuntimeModel):
    id: str
    kind: ArtifactKind
    producer: str
    step_id: str
    revision: int = Field(ge=1)
    parent_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    content_sha256: str
    created_at: datetime


class PlanStep(RuntimeModel):
    id: str = Field(min_length=1, max_length=128)
    capability: AgentCapability
    objective: str = Field(min_length=1, max_length=2_000)
    output_kind: ArtifactKind
    input_kinds: tuple[ArtifactKind, ...] = ()
    dependencies: tuple[str, ...] = ()
    attempt: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=2, ge=1, le=5)

    @model_validator(mode="after")
    def validate_dependencies(self) -> "PlanStep":
        if self.id in self.dependencies:
            raise ValueError("A plan step cannot depend on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Plan step dependencies must be unique")
        return self


class PlanSnapshot(RuntimeModel):
    revision: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2_000)
    steps: tuple[PlanStep, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_steps(self) -> "PlanSnapshot":
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Plan step IDs must be unique")
        known = set(step_ids)
        for step in self.steps:
            unknown = set(step.dependencies) - known
            if unknown:
                raise ValueError(
                    f"Plan step {step.id} has unknown dependencies: {sorted(unknown)}"
                )
        return self


class AgentUsage(RuntimeModel):
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)


class BudgetLimits(RuntimeModel):
    max_supervisor_turns: int = Field(default=24, ge=1)
    max_agent_dispatches: int = Field(default=24, ge=1)
    max_model_calls: int = Field(default=24, ge=1)
    max_output_tokens: int = Field(default=40_000, ge=1)
    max_replans: int = Field(default=4, ge=0)
    max_writer_revisions: int = Field(default=2, ge=0)
    specialist_timeout_seconds: float = Field(default=90.0, gt=0.0)


class BudgetUsage(RuntimeModel):
    supervisor_turns: int = Field(default=0, ge=0)
    agent_dispatches: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    replans: int = Field(default=0, ge=0)
    writer_revisions: int = Field(default=0, ge=0)


class FactDraft(RuntimeModel):
    key: str = Field(min_length=1, max_length=256)
    value: Any
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class FactRecord(RuntimeModel):
    id: str
    key: str
    value: Any
    source_artifact_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=utc_now)


class ProgressEntry(RuntimeModel):
    sequence_key: str
    type: str
    message: str
    step_id: str | None = None
    agent: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class StepExecution(RuntimeModel):
    id: str
    plan_revision: int = Field(ge=1)
    step_id: str
    capability: AgentCapability
    agent: str
    status: PlanStepStatus
    attempt: int = Field(ge=1)
    artifact_ids: tuple[str, ...] = ()
    usage: AgentUsage = Field(default_factory=AgentUsage)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    started_at: datetime
    finished_at: datetime


class RuntimeFailure(RuntimeModel):
    code: str
    message: str
    retryable: bool = False
    step_id: str | None = None
    agent: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class HumanDecisionRequest(RuntimeModel):
    kind: CreatorDecisionKind
    prompt: str
    source_artifact_id: str
    allowed_actions: tuple[CreatorDecisionAction, ...]
    allowed_option_ids: tuple[str, ...] = ()


class HumanInterruptPayload(RuntimeModel):
    decision_id: str
    kind: CreatorDecisionKind
    prompt: str
    source_artifact_id: str
    allowed_actions: tuple[CreatorDecisionAction, ...]
    allowed_option_ids: tuple[str, ...] = ()
    options: tuple[dict[str, Any], ...] = ()


class SupervisorDecision(RuntimeModel):
    action: SupervisorAction
    reason: str
    dispatch_step_ids: tuple[str, ...] = ()
    final_source_artifact_id: str | None = None
    human_request: HumanDecisionRequest | None = None
    failure: RuntimeFailure | None = None

    @model_validator(mode="after")
    def validate_action_payload(self) -> "SupervisorDecision":
        if self.action == SupervisorAction.DISPATCH and not self.dispatch_step_ids:
            raise ValueError("DISPATCH requires at least one step")
        if self.action == SupervisorAction.REQUEST_HUMAN and self.human_request is None:
            raise ValueError("REQUEST_HUMAN requires a human request")
        if self.action == SupervisorAction.FINISH and not self.final_source_artifact_id:
            raise ValueError("FINISH requires a final source artifact")
        if self.action == SupervisorAction.FAIL and self.failure is None:
            raise ValueError("FAIL requires a failure")
        return self


class AgentDescriptor(RuntimeModel):
    name: str
    capabilities: frozenset[AgentCapability]
    description: str


class AgentExecutionContext(RuntimeModel):
    identity: RunIdentity
    goal: CreatorGoal
    plan_revision: int
    step: PlanStep
    artifacts: tuple[CreatorArtifact, ...] = ()
    facts: tuple[FactRecord, ...] = ()


class AgentResult(RuntimeModel):
    artifacts: tuple[ArtifactPayload, ...]
    facts: tuple[FactDraft, ...] = ()
    usage: AgentUsage = Field(default_factory=AgentUsage)
    summary: str = Field(min_length=1, max_length=2_000)


def merge_indexed(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> dict[str, Any]:
    merged = dict(left or {})
    for key, value in (right or {}).items():
        existing = merged.get(key)
        if existing is not None and existing != value:
            raise ValueError(f"Conflicting parallel state update for key {key}")
        merged[key] = value
    return merged


def append_items(
    left: tuple[Any, ...] | None, right: tuple[Any, ...] | None
) -> tuple[Any, ...]:
    return tuple(left or ()) + tuple(right or ())


def add_budget_usage(
    left: BudgetUsage | None, right: BudgetUsage | None
) -> BudgetUsage:
    first = left or BudgetUsage()
    second = right or BudgetUsage()
    return BudgetUsage(
        supervisor_turns=first.supervisor_turns + second.supervisor_turns,
        agent_dispatches=first.agent_dispatches + second.agent_dispatches,
        model_calls=first.model_calls + second.model_calls,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        tool_calls=first.tool_calls + second.tool_calls,
        replans=first.replans + second.replans,
        writer_revisions=first.writer_revisions + second.writer_revisions,
    )


class CreatorGraphState(TypedDict):
    identity: RunIdentity
    goal: CreatorGoal
    limits: BudgetLimits
    usage: Annotated[BudgetUsage, add_budget_usage]
    plan: PlanSnapshot | None
    plan_history: Annotated[tuple[PlanSnapshot, ...], append_items]
    executions: Annotated[dict[str, StepExecution], merge_indexed]
    artifacts: Annotated[dict[str, ArtifactRef], merge_indexed]
    facts: Annotated[dict[str, FactRecord], merge_indexed]
    progress: Annotated[tuple[ProgressEntry, ...], append_items]
    errors: Annotated[tuple[RuntimeFailure, ...], append_items]
    decision: SupervisorDecision | None
    control_status: RuntimeControlStatus
    final_artifact_id: str | None
    pending_decision_artifact_id: str | None
    applied_decision_id: str | None


class AgentDispatchEnvelope(TypedDict):
    identity: RunIdentity
    goal: CreatorGoal
    plan_revision: int
    step: PlanStep
    artifact_refs: tuple[ArtifactRef, ...]
    facts: tuple[FactRecord, ...]
