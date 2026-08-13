from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.creator.domain.models import CreatorTaskKind, CreatorTaskStatus
from app.creator.runtime.models import AgentCapability, ArtifactKind, PlanStepStatus
from app.creator.tools.models import CreatorToolCallStatus


def utc_now() -> datetime:
    return datetime.now(UTC)


class EvaluationModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvaluationMode(str, Enum):
    OFFLINE_REGRESSION = "OFFLINE_REGRESSION"
    ONLINE_SAMPLE = "ONLINE_SAMPLE"
    MANUAL_AUDIT = "MANUAL_AUDIT"


class EvaluationOutcome(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


class EvaluationMetricStatus(str, Enum):
    SCORED = "SCORED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class EvaluationMetricName(str, Enum):
    RETRIEVAL_RECALL_AT_K = "retrieval_recall_at_k"
    RETRIEVAL_PRECISION_AT_K = "retrieval_precision_at_k"
    RETRIEVAL_MRR = "retrieval_mrr"
    RETRIEVAL_NDCG_AT_K = "retrieval_ndcg_at_k"
    RETRIEVAL_ACL_SAFETY = "retrieval_acl_safety"
    AGENT_TASK_SUCCESS_RATE = "agent_task_success_rate"
    AGENT_TOOL_CALLING_ACCURACY = "agent_tool_calling_accuracy"
    AGENT_PLANNING_QUALITY = "agent_planning_quality"
    GENERATION_FAITHFULNESS = "generation_faithfulness"
    GENERATION_RELEVANCE = "generation_relevance"
    GENERATION_STYLE_CONSISTENCY = "generation_style_consistency"


class ClaimVerdict(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_ASSESSABLE = "NOT_ASSESSABLE"


ALL_EVALUATION_METRICS: tuple[EvaluationMetricName, ...] = tuple(EvaluationMetricName)


class EvaluationThresholds(EvaluationModel):
    retrieval_k: int = Field(default=6, ge=1, le=100)
    retrieval_recall_at_k: float = Field(default=0.70, ge=0.0, le=1.0)
    retrieval_precision_at_k: float = Field(default=0.50, ge=0.0, le=1.0)
    retrieval_mrr: float = Field(default=0.50, ge=0.0, le=1.0)
    retrieval_ndcg_at_k: float = Field(default=0.70, ge=0.0, le=1.0)
    retrieval_acl_safety: float = Field(default=1.0, ge=0.0, le=1.0)
    agent_task_success_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    agent_tool_calling_accuracy: float = Field(default=0.80, ge=0.0, le=1.0)
    agent_planning_quality: float = Field(default=0.75, ge=0.0, le=1.0)
    generation_faithfulness: float = Field(default=0.80, ge=0.0, le=1.0)
    generation_relevance: float = Field(default=0.70, ge=0.0, le=1.0)
    generation_style_consistency: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
    )

    def for_metric(self, metric: EvaluationMetricName) -> float:
        return float(getattr(self, metric.value))


class ToolExpectation(EvaluationModel):
    name: str = Field(min_length=1, max_length=128)
    arguments_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    min_calls: int = Field(default=1, ge=1, le=20)
    max_calls: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def validate_call_range(self) -> ToolExpectation:
        if self.max_calls is not None and self.max_calls < self.min_calls:
            raise ValueError("max_calls cannot be smaller than min_calls")
        return self


class StyleCriteria(EvaluationModel):
    instructions: tuple[str, ...] = Field(default=(), max_length=20)
    required_terms: tuple[str, ...] = Field(default=(), max_length=50)
    forbidden_terms: tuple[str, ...] = Field(default=(), max_length=50)
    required_headings: tuple[str, ...] = Field(default=(), max_length=20)
    exemplar_texts: tuple[str, ...] = Field(default=(), max_length=5)
    min_chars: int | None = Field(default=None, ge=1, le=500_000)
    max_chars: int | None = Field(default=None, ge=1, le=500_000)

    @model_validator(mode="after")
    def validate_style_rules(self) -> StyleCriteria:
        values = (
            *self.instructions,
            *self.required_terms,
            *self.forbidden_terms,
            *self.required_headings,
        )
        if any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("Style criteria values must contain 1-2000 characters")
        if any(
            not value.strip() or len(value) > 50_000 for value in self.exemplar_texts
        ):
            raise ValueError("Style exemplars must contain 1-50000 characters")
        if (
            self.min_chars is not None
            and self.max_chars is not None
            and self.min_chars > self.max_chars
        ):
            raise ValueError("min_chars cannot exceed max_chars")
        return self


class EvaluationCriteria(EvaluationModel):
    relevant_document_ids: tuple[str, ...] = Field(default=(), max_length=100)
    expected_tools: tuple[ToolExpectation, ...] = Field(default=(), max_length=50)
    expected_capabilities: tuple[AgentCapability, ...] = Field(
        default=(),
        max_length=30,
    )
    required_concepts: tuple[str, ...] = Field(default=(), max_length=100)
    reference_answer: str | None = Field(default=None, max_length=100_000)
    expected_final_artifact_kind: ArtifactKind | None = None
    style: StyleCriteria = Field(default_factory=StyleCriteria)
    allow_additional_tools: bool = False
    max_plan_steps: int = Field(default=24, ge=1, le=100)
    max_replans: int = Field(default=4, ge=0, le=20)
    required_metrics: tuple[EvaluationMetricName, ...] = Field(
        default=ALL_EVALUATION_METRICS,
        min_length=1,
    )
    thresholds: EvaluationThresholds = Field(default_factory=EvaluationThresholds)

    @model_validator(mode="after")
    def validate_criteria(self) -> EvaluationCriteria:
        collections = (
            self.relevant_document_ids,
            self.expected_capabilities,
            self.required_concepts,
            self.required_metrics,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError(
                "Evaluation criteria collections must not contain duplicates"
            )
        tool_keys = [(item.name, item.arguments_sha256) for item in self.expected_tools]
        if len(tool_keys) != len(set(tool_keys)):
            raise ValueError("Tool expectations must be unique")
        if any(not item.strip() for item in self.relevant_document_ids):
            raise ValueError("Relevant document IDs cannot be blank")
        if any(not item.strip() for item in self.required_concepts):
            raise ValueError("Required concepts cannot be blank")
        return self


class EvaluationCase(EvaluationModel):
    id: str = Field(min_length=1, max_length=128)
    task_kind: CreatorTaskKind
    goal: str = Field(min_length=1, max_length=20_000)
    criteria: EvaluationCriteria
    split: str = Field(default="regression", min_length=1, max_length=64)
    tags: tuple[str, ...] = Field(default=(), max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvaluationDataset(EvaluationModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.[0-9]+$")
    id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4_000)
    cases: tuple[EvaluationCase, ...] = Field(min_length=1, max_length=10_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_cases(self) -> EvaluationDataset:
        case_ids = [case.id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Evaluation case IDs must be unique")
        return self


class ObservedEvidence(EvaluationModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    document_id: str = Field(min_length=1, max_length=128)
    rank: int = Field(ge=1)
    text: str = Field(default="", max_length=20_000)
    source: str | None = Field(default=None, max_length=2_000)
    authority_verified: bool = True


class ObservedPlanStep(EvaluationModel):
    step_id: str = Field(min_length=1, max_length=128)
    capability: AgentCapability
    dependencies: tuple[str, ...] = Field(default=(), max_length=30)


class ObservedPlan(EvaluationModel):
    revision: int = Field(ge=1)
    reason: str = Field(default="", max_length=2_000)
    steps: tuple[ObservedPlanStep, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def validate_step_ids(self) -> ObservedPlan:
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Observed plan step IDs must be unique")
        return self


class ObservedExecution(EvaluationModel):
    execution_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    capability: AgentCapability
    agent: str = Field(min_length=1, max_length=128)
    status: PlanStepStatus
    error_code: str | None = Field(default=None, max_length=128)


class ObservedToolCall(EvaluationModel):
    call_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    status: CreatorToolCallStatus
    arguments_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    latency_ms: int | None = Field(default=None, ge=0)
    error_code: str | None = Field(default=None, max_length=128)


class ClaimAssessment(EvaluationModel):
    claim: str = Field(min_length=1, max_length=4_000)
    verdict: ClaimVerdict
    supporting_evidence_ids: tuple[str, ...] = Field(default=(), max_length=20)
    reason: str = Field(default="", max_length=2_000)

    @model_validator(mode="after")
    def validate_support(self) -> ClaimAssessment:
        if len(self.supporting_evidence_ids) != len(set(self.supporting_evidence_ids)):
            raise ValueError("Supporting evidence IDs must be unique")
        if self.verdict == ClaimVerdict.SUPPORTED and not self.supporting_evidence_ids:
            raise ValueError("Supported claims require at least one evidence ID")
        return self


class GenerationObservation(EvaluationModel):
    title: str = Field(default="", max_length=512)
    body_markdown: str = Field(default="", max_length=500_000)
    cited_evidence_ids: tuple[str, ...] = Field(default=(), max_length=100)
    declared_unsupported_claims: tuple[str, ...] = Field(default=(), max_length=100)
    claim_assessments: tuple[ClaimAssessment, ...] = Field(
        default=(),
        max_length=500,
    )


class CreatorEvaluationObservation(EvaluationModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.[0-9]+$")
    case_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    task_status: CreatorTaskStatus
    goal: str = Field(min_length=1, max_length=20_000)
    final_artifact_kind: ArtifactKind | None = None
    evidence: tuple[ObservedEvidence, ...] = Field(default=(), max_length=100)
    plans: tuple[ObservedPlan, ...] = Field(default=(), max_length=20)
    executions: tuple[ObservedExecution, ...] = Field(default=(), max_length=500)
    tool_calls: tuple[ObservedToolCall, ...] = Field(default=(), max_length=1_000)
    generation: GenerationObservation | None = None
    replan_count: int = Field(default=0, ge=0)
    runtime_error_codes: tuple[str, ...] = Field(default=(), max_length=100)
    limitations: tuple[str, ...] = Field(default=(), max_length=100)
    captured_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_observation(self) -> CreatorEvaluationObservation:
        evidence_ids = [item.evidence_id for item in self.evidence]
        ranks = [item.rank for item in self.evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("Observed evidence IDs must be unique")
        if len(ranks) != len(set(ranks)):
            raise ValueError("Observed evidence ranks must be unique")
        revisions = [plan.revision for plan in self.plans]
        if len(revisions) != len(set(revisions)):
            raise ValueError("Observed plan revisions must be unique")
        execution_ids = [item.execution_id for item in self.executions]
        if len(execution_ids) != len(set(execution_ids)):
            raise ValueError("Observed execution IDs must be unique")
        call_ids = [item.call_id for item in self.tool_calls]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Observed tool call IDs must be unique")
        return self


class EvaluationObservationSet(EvaluationModel):
    schema_version: str = Field(default="1.0", pattern=r"^1\.[0-9]+$")
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    observations: tuple[CreatorEvaluationObservation, ...] = Field(
        min_length=1,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def validate_case_ids(self) -> EvaluationObservationSet:
        case_ids = [observation.case_id for observation in self.observations]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Observation case IDs must be unique")
        return self


class JudgeMetricScore(EvaluationModel):
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2_000)


class GenerationJudgeAssessment(EvaluationModel):
    judge_name: str = Field(min_length=1, max_length=128)
    judge_version: str = Field(min_length=1, max_length=128)
    faithfulness: JudgeMetricScore | None = None
    relevance: JudgeMetricScore | None = None
    style_consistency: JudgeMetricScore | None = None
    claims: tuple[ClaimAssessment, ...] = Field(default=(), max_length=500)
    limitations: tuple[str, ...] = Field(default=(), max_length=50)

    @model_validator(mode="after")
    def validate_faithfulness_claims(self) -> GenerationJudgeAssessment:
        if self.faithfulness is not None and not self.claims:
            raise ValueError("Faithfulness assessment requires claim-level verdicts")
        return self


class EvaluationMetricResult(EvaluationModel):
    metric: EvaluationMetricName
    status: EvaluationMetricStatus
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    threshold: float = Field(ge=0.0, le=1.0)
    passed: bool | None = None
    evaluator: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2_000)
    details: dict[str, Any] = Field(default_factory=dict)
    sample_size: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def validate_result(self) -> EvaluationMetricResult:
        if self.status == EvaluationMetricStatus.SCORED:
            if self.score is None or self.passed is None:
                raise ValueError("Scored metrics require score and passed")
        elif self.score is not None or self.passed is not None:
            raise ValueError("Skipped or errored metrics cannot carry a score")
        return self


class EvaluationCaseReport(EvaluationModel):
    case_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    trace_id: str = Field(min_length=1, max_length=128)
    outcome: EvaluationOutcome
    passed: bool
    overall_score: float | None = Field(default=None, ge=0.0, le=1.0)
    metrics: tuple[EvaluationMetricResult, ...] = Field(min_length=1)
    required_metrics: tuple[EvaluationMetricName, ...] = Field(min_length=1)
    observation_sha256: str = Field(min_length=64, max_length=64)
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_metrics(self) -> EvaluationCaseReport:
        names = [metric.metric for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("Case report metrics must be unique")
        if self.outcome != EvaluationOutcome.PASSED and self.passed:
            raise ValueError("Only PASSED case reports may set passed=true")
        return self


class EvaluationRunReport(EvaluationModel):
    id: str = Field(min_length=1, max_length=64)
    tenant_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    mode: EvaluationMode
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    dataset_sha256: str = Field(min_length=64, max_length=64)
    request_sha256: str = Field(min_length=64, max_length=64)
    candidate_name: str = Field(min_length=1, max_length=128)
    candidate_version: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=128)
    baseline_evaluation_run_id: str | None = Field(default=None, max_length=64)
    metric_deltas: dict[str, float] = Field(default_factory=dict)
    outcome: EvaluationOutcome
    passed: bool
    overall_score: float | None = Field(default=None, ge=0.0, le=1.0)
    metrics: tuple[EvaluationMetricResult, ...] = Field(min_length=1)
    cases: tuple[EvaluationCaseReport, ...] = Field(min_length=1)
    limitations: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def validate_report(self) -> EvaluationRunReport:
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("Evaluation report case IDs must be unique")
        names = [metric.metric for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("Aggregate metric names must be unique")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.outcome != EvaluationOutcome.PASSED and self.passed:
            raise ValueError("Only PASSED evaluation reports may set passed=true")
        return self


class EvaluationExecutionResult(EvaluationModel):
    report: EvaluationRunReport
    replayed: bool = False


class RuntimeEvaluationSummary(EvaluationModel):
    task_success: bool
    quality_score: float = Field(ge=0.0, le=1.0)
    metric_status: str = Field(min_length=1, max_length=64)
    dataset_id: str = Field(min_length=1, max_length=128)
    dataset_version: str = Field(min_length=1, max_length=128)
    evaluator_version: str = Field(min_length=1, max_length=512)
    metrics: tuple[EvaluationMetricResult, ...] = Field(min_length=1)
    unevaluated_metrics: tuple[EvaluationMetricName, ...] = ()
    planning_observations: tuple[str, ...] = ()
    generation_observations: tuple[str, ...] = ()


class EvaluationSnapshotRequest(EvaluationModel):
    case_id: str = Field(min_length=1, max_length=128)
    tenant_id: str = Field(min_length=1, max_length=128)
    creator_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
