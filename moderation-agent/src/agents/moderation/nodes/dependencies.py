from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from agents.moderation.routes import ModerationThresholds
from community.tools import CommunityContextLoader, EmptyCommunityContextLoader
from moderation.schemas import (
    AdversarialAgentMetrics,
    AgentDecision,
    AgenticPolicyRAGConfig,
    CaseEvidence,
    EvidenceReviewerConfig,
    EvidenceReviewerDecision,
    EvidenceReviewerMetrics,
    JudgeAgentResult,
    ModerationContentType,
    ModerationContextEvidence,
    ModerationSignalEvidence,
    PolicyEvidence,
    PolicyGradeResult,
    PolicyQueryPlan,
    RejectedPolicy,
    RetrievedPolicy,
    RewrittenPolicyQuery,
    RiskAgentResult,
    RiskClassification,
    RiskType,
    SafeAgentResult,
    ToolAgentMetrics,
    ToolCallingConfig,
)
from moderation.services.preflight import ModerationPreflightService, PreflightConfig
from rag.cases import CaseRetriever, EmptyCaseRetriever
from rag.policy import InMemoryPolicyRetriever, PolicyRetriever

if TYPE_CHECKING:
    from agents.moderation.state import ModerationState
    from rag.policy.agentic import PolicyRetrievalBatch


class RiskClassifier(Protocol):
    async def classify(
        self,
        *,
        content: str,
        content_type: ModerationContentType,
        context: ModerationContextEvidence | None,
        signals: list[ModerationSignalEvidence],
        config: RunnableConfig,
    ) -> RiskClassification: ...


class DecisionJudge(Protocol):
    async def decide(
        self,
        *,
        content: str,
        content_type: ModerationContentType,
        classification: RiskClassification,
        policies: list[PolicyEvidence],
        cases: list[CaseEvidence],
        context: ModerationContextEvidence | None,
        signals: list[ModerationSignalEvidence],
        evidence_summary: dict[str, Any] | None,
        config: RunnableConfig,
    ) -> AgentDecision: ...


@dataclass(frozen=True)
class PolicyPlannerCall:
    plan: PolicyQueryPlan
    fallback_used: bool = False
    error: str | None = None


class PolicyQueryPlanner(Protocol):
    async def plan(
        self,
        *,
        content: str,
        classification: RiskClassification,
        signals: list[ModerationSignalEvidence],
        risk_hypotheses: list[RiskType],
        evidence_summary: dict[str, Any] | None,
        preliminary_policies: list[PolicyEvidence],
        config: RunnableConfig,
    ) -> PolicyPlannerCall: ...


class AgenticPolicyRetrieverPort(Protocol):
    async def retrieve(
        self,
        *,
        plan: PolicyQueryPlan,
        platform: str,
        retrieval_round: int,
    ) -> "PolicyRetrievalBatch": ...


@dataclass(frozen=True)
class PolicyGraderCall:
    result: PolicyGradeResult
    considered_policies: tuple[RetrievedPolicy, ...]
    rejected_policies: tuple[RejectedPolicy, ...] = ()
    fallback_used: bool = False
    errors: tuple[str, ...] = ()


class PolicyGrader(Protocol):
    async def grade(
        self,
        *,
        content: str,
        classification: RiskClassification,
        signals: list[ModerationSignalEvidence],
        evidence_summary: dict[str, Any] | None,
        plan: PolicyQueryPlan,
        policies: list[RetrievedPolicy],
        config: RunnableConfig,
    ) -> PolicyGraderCall: ...


@dataclass(frozen=True)
class PolicyRewriterCall:
    rewritten: RewrittenPolicyQuery
    fallback_used: bool = False
    error: str | None = None


class PolicyQueryRewriter(Protocol):
    async def rewrite(
        self,
        *,
        plan: PolicyQueryPlan,
        grade_result: PolicyGradeResult,
        retrieved_policies: list[RetrievedPolicy],
        retrieval_round: int,
        config: RunnableConfig,
    ) -> PolicyRewriterCall: ...


@dataclass(frozen=True)
class ToolAgentCall:
    message: AIMessage
    metrics: ToolAgentMetrics


class ModerationToolCallingAgent(Protocol):
    async def invoke(
        self,
        *,
        messages: list[AnyMessage],
        tools: list[BaseTool],
        state: "ModerationState",
        config: RunnableConfig,
    ) -> ToolAgentCall: ...


@dataclass(frozen=True)
class AdversarialReviewInput:
    content: str
    content_hash: str | None
    content_type: ModerationContentType
    classification: RiskClassification
    policies: tuple[PolicyEvidence, ...]
    cases: tuple[CaseEvidence, ...]
    context: ModerationContextEvidence | None
    signals: tuple[ModerationSignalEvidence, ...]
    evidence_summary: dict[str, Any] | None = None


@dataclass(frozen=True)
class AdversarialAgentCall[ResultT]:
    result: ResultT
    metrics: AdversarialAgentMetrics


class RiskInvestigator(Protocol):
    async def investigate(
        self,
        *,
        review_input: AdversarialReviewInput,
        config: RunnableConfig,
    ) -> AdversarialAgentCall[RiskAgentResult]: ...


class SafeAdvocate(Protocol):
    async def advocate(
        self,
        *,
        review_input: AdversarialReviewInput,
        config: RunnableConfig,
    ) -> AdversarialAgentCall[SafeAgentResult]: ...


class AdversarialDecisionJudge(Protocol):
    async def decide_adversarial(
        self,
        *,
        review_input: AdversarialReviewInput,
        risk_result: RiskAgentResult | None,
        safe_result: SafeAgentResult | None,
        agent_conflict: bool,
        agent_errors: tuple[str, ...],
        config: RunnableConfig,
    ) -> AdversarialAgentCall[JudgeAgentResult]: ...


@dataclass(frozen=True)
class EvidenceReviewInput:
    content: str
    content_hash: str | None
    content_type: ModerationContentType
    classification: RiskClassification
    decision: AgentDecision
    policies: tuple[PolicyEvidence, ...]
    cases: tuple[CaseEvidence, ...]
    context: ModerationContextEvidence | None
    signals: tuple[ModerationSignalEvidence, ...]
    evidence_summary: dict[str, Any] | None = None
    policy_evidence_summary: dict[str, Any] | None = None
    risk_result: RiskAgentResult | None = None
    safe_result: SafeAgentResult | None = None
    judge_result: JudgeAgentResult | None = None
    agent_conflict: bool = False
    agent_errors: tuple[str, ...] = ()
    evidence_check_passed: bool = False
    evidence_check_issues: tuple[dict[str, Any], ...] = ()
    reviewer_iteration: int = 1


@dataclass(frozen=True)
class EvidenceReviewerCall:
    decision: EvidenceReviewerDecision
    metrics: EvidenceReviewerMetrics


class EvidenceReviewer(Protocol):
    async def review(
        self,
        *,
        review_input: EvidenceReviewInput,
        config: RunnableConfig,
    ) -> EvidenceReviewerCall: ...


@dataclass
class ModerationDependencies:
    classifier: RiskClassifier
    judge: DecisionJudge
    policy_retriever: PolicyRetriever = field(default_factory=InMemoryPolicyRetriever)
    case_retriever: CaseRetriever = field(default_factory=EmptyCaseRetriever)
    context_loader: CommunityContextLoader = field(default_factory=EmptyCommunityContextLoader)
    tool_agent: ModerationToolCallingAgent | None = None
    tool_calling_config: ToolCallingConfig = field(default_factory=ToolCallingConfig)
    risk_investigator: RiskInvestigator | None = None
    safe_advocate: SafeAdvocate | None = None
    adversarial_judge: AdversarialDecisionJudge | None = None
    policy_query_planner: PolicyQueryPlanner | None = None
    agentic_policy_retriever: AgenticPolicyRetrieverPort | None = None
    policy_grader: PolicyGrader | None = None
    policy_query_rewriter: PolicyQueryRewriter | None = None
    policy_rag_config: AgenticPolicyRAGConfig = field(default_factory=AgenticPolicyRAGConfig)
    evidence_reviewer: EvidenceReviewer | None = None
    evidence_reviewer_config: EvidenceReviewerConfig = field(
        default_factory=lambda: EvidenceReviewerConfig(enabled=False)
    )
    thresholds: ModerationThresholds = field(default_factory=ModerationThresholds)
    low_risk_fast_path_enabled: bool = False
    adaptive_cascade_enabled: bool = False
    policy_engine_enabled: bool = False
    preflight: ModerationPreflightService | None = None
    # Off by default for unit graphs; production wires settings.moderation_preflight_config().
    preflight_config: PreflightConfig = field(
        default_factory=lambda: PreflightConfig(l0_enabled=False, l1_enabled=False)
    )
