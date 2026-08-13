"""GreenBook behavioral evaluation contracts and runner."""

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

__all__ = [
    "AgentEvaluationMetrics",
    "AgentMetricsCalculator",
    "EvalCase",
    "EvalResult",
    "EvaluationMetricsCalculator",
    "EvaluationReport",
    "EvaluationRunner",
    "EvaluationTrace",
    "FailureCategory",
    "GOLDEN_CASES",
    "golden_cases",
]
