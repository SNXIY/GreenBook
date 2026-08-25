"""GreenBook behavioral evaluation contracts and runner."""

from .badcase import BadCaseStore, CaseLevelStatus
from .business import BusinessAcceptanceEvaluator, BusinessAcceptanceReport, BusinessAcceptanceRun
from .canonical import canonical_semantic_result, semantic_mapping_matches
from .cases.business_acceptance import BUSINESS_ACCEPTANCE_CASES, business_acceptance_cases
from .cases.semantic_baseline import SEMANTIC_BASELINE_CASES, semantic_baseline_cases
from .dataset import GOLDEN_CASES, golden_cases
from .metrics import AgentMetricsCalculator, EvaluationMetricsCalculator
from .models import (
    AgentEvaluationMetrics,
    EvalCase,
    EvalResult,
    EvaluationReport,
    EvaluationTrace,
    FailureCategory,
)
from .runner import EvaluationRunner
from .runtime_adapter import (
    HttpRuntimeTransport,
    RuntimeEvaluationAdapter,
    RuntimeEvaluationTransport,
)
from .semantic import ProductionSemanticAdapter, SemanticEvaluator
from .stability import (
    ProductionSemanticStabilityEvaluator,
    SemanticStabilityCaseResult,
    SemanticStabilityReport,
    SemanticStabilityRun,
    StabilityClassification,
    aggregate_case_stability,
    aggregate_stability_report,
    stable_fingerprint,
)

__all__ = [
    "AgentEvaluationMetrics",
    "AgentMetricsCalculator",
    "BusinessAcceptanceEvaluator",
    "BusinessAcceptanceReport",
    "BusinessAcceptanceRun",
    "BUSINESS_ACCEPTANCE_CASES",
    "canonical_semantic_result",
    "BadCaseStore",
    "CaseLevelStatus",
    "EvalCase",
    "EvalResult",
    "EvaluationMetricsCalculator",
    "EvaluationReport",
    "EvaluationRunner",
    "EvaluationTrace",
    "FailureCategory",
    "HttpRuntimeTransport",
    "GOLDEN_CASES",
    "ProductionSemanticAdapter",
    "RuntimeEvaluationAdapter",
    "RuntimeEvaluationTransport",
    "SemanticEvaluator",
    "ProductionSemanticStabilityEvaluator",
    "SemanticStabilityCaseResult",
    "SemanticStabilityReport",
    "SemanticStabilityRun",
    "StabilityClassification",
    "aggregate_case_stability",
    "aggregate_stability_report",
    "semantic_mapping_matches",
    "stable_fingerprint",
    "golden_cases",
    "SEMANTIC_BASELINE_CASES",
    "semantic_baseline_cases",
    "business_acceptance_cases",
]
