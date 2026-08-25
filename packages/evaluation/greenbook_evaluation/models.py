"""Evaluation models — EvalCase, EvalResult, EvaluationReport."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    """One evaluation test case."""

    case_id: str
    category: str               # COMMAND | GOAL | REFERENCE | PLAN | EXECUTION
    description: str = ""       # human-readable
    user_message: str = ""
    conversation_turns: list[dict[str, Any]] = Field(default_factory=list)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    # Explicit semantic-only setup.  It is passed as bounded context to the
    # production interpreter; it never mutates Java or durable Runtime state.
    setup_context: dict[str, Any] = Field(default_factory=dict)

    # ── conversation context and expected outputs ──
    existing_tasks: list[dict] = []
    expected_tools: list[str] | None = None          # tool names called
    expected_resource_id: str | None = None           # draft_id or schedule_id
    expected_reference_task_id: str | None = None
    expected_command: str | None = None
    expected_target: dict[str, Any] | None = None
    expected_semantic_state: dict[str, Any] | None = None
    expected_objective_count: int | None = None
    expected_temporal_resolution: bool | None = None
    expected_goals: list[str] = Field(default_factory=list)
    expected_task_state: str | None = None
    # Runtime-only expectations.  The existing semantic fixture does not need
    # to populate these fields; the thin Runtime adapter uses them for the
    # small representative smoke set.
    expected_terminal_status: str | None = None
    expected_resource_types: list[str] = Field(default_factory=list)
    expected_schedule: dict[str, Any] | None = None
    expected_approval: str | bool | None = None
    expected_artifacts: list[str] = Field(default_factory=list)
    expected_side_effects: list[str] = Field(default_factory=list)
    expected_duplicate_write_count: int | None = None
    expected_ownership_conflicts: int | None = None
    forbidden_actions: list[str] = Field(default_factory=list)

    # ── expected outcome ──
    should_succeed: bool = True
    expected_status: str = "COMPLETED"               # COMPLETED | FAILED | WAITING_APPROVAL | PARTIAL | SKIPPED
    expected_clarification: bool | None = None       # True when needs_clarification
    expected_trace_events: list[str] | None = None   # [TASK_CREATED, TOOL_INVOKED, …]


class EvalCheck(BaseModel):
    """One individual check within an EvalResult."""
    check: str = ""              # "command.type" | "goals" | "tool"
    expected: object = None
    actual: object = None
    ok: bool = False


class EvalResult(BaseModel):
    """Result of running one EvalCase."""
    case_id: str = ""
    category: str = ""
    description: str = ""
    user_message: str = ""
    passed: bool = False
    checks: list[EvalCheck] = []
    errors: list[str] = []
    duration_ms: float = 0.0
    trace_summary: dict = Field(default_factory=dict)
    # {event_count, tool_count, step_count}
    trace: dict[str, Any] = Field(default_factory=dict)
    failure_categories: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class CategoryMetrics(BaseModel):
    """Per-category accuracy."""
    category: str = ""
    total: int = 0
    passed: int = 0
    accuracy: float = 0.0
    failures: list[str] = []


class EvaluationReport(BaseModel):
    """Complete evaluation report."""
    run_id: str = ""
    total_cases: int = 0
    total_passed: int = 0
    overall_accuracy: float = 0.0
    by_category: dict[str, CategoryMetrics] = Field(default_factory=dict)
    duration_ms: float = 0.0
    results: list[EvalResult] = []
    bad_cases: list = Field(default_factory=list)  # list[BadCase]
    failure_summary: dict[str, int] = Field(default_factory=dict)
    metrics: dict[str, float] = Field(default_factory=dict)
    # Case-level bookkeeping is deliberately separate from the historical
    # assertion list above.  One EvalCase has one status even when it has
    # several failed assertions.
    case_level_statuses: dict[str, str] = Field(default_factory=dict)
    case_level_counts: dict[str, int] = Field(default_factory=dict)
    open_assertion_count: int = 0


class FailureCategory(StrEnum):
    """Behavioral failure taxonomy for Agent Runtime evaluation."""

    COMMAND_ERROR = "COMMAND_ERROR"
    TARGET_ERROR = "TARGET_ERROR"
    GOAL_ERROR = "GOAL_ERROR"
    PLAN_ERROR = "PLAN_ERROR"
    TOOL_SELECTION_ERROR = "TOOL_SELECTION_ERROR"
    TOOL_ARGUMENT_ERROR = "TOOL_ARGUMENT_ERROR"
    POLICY_ERROR = "POLICY_ERROR"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    RECOVERY_ERROR = "RECOVERY_ERROR"
    MEMORY_ERROR = "MEMORY_ERROR"
    HALLUCINATION = "HALLUCINATION"
    UNNECESSARY_CLARIFICATION = "UNNECESSARY_CLARIFICATION"
    MISSING_CLARIFICATION = "MISSING_CLARIFICATION"


class EvaluationTrace(BaseModel):
    """The same correlation identifiers used by Runtime observability."""

    conversation_id: str = ""
    task_id: str = ""
    goal_id: str = ""
    plan_version: int = 0
    execution_id: str = ""
    step_id: str = ""
    tool_name: str = ""
    context_snapshot_id: str = ""
    memory_ids: list[str] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)


class AgentEvaluationMetrics(BaseModel):
    """Behavioral metrics, reported as rates unless named otherwise."""

    command_accuracy: float = 0.0
    target_resolution_accuracy: float = 0.0
    goal_decomposition_accuracy: float = 0.0
    tool_selection_accuracy: float = 0.0
    task_success_rate: float = 0.0
    task_completion_rate: float = 0.0
    plan_quality: float = 0.0
    plan_success_rate: float = 0.0
    recovery_success: float = 0.0
    replan_recovery_rate: float = 0.0
    multi_task_accuracy: float = 0.0
    long_conversation_consistency: float = 0.0
    clarification_precision: float = 0.0
    side_effect_safety: float = 0.0
    idempotent_recovery: float = 0.0
    memory_retrieval_precision: float = 0.0
    context_continuity: float = 0.0
    average_latency_ms: float = 0.0
    average_tool_call_count: float = 0.0
    objective_success_rate: float = 0.0
    duplicate_write_count: float = 0.0
    clarification_accuracy: float = 0.0
    avg_actionloop_iterations: float = 0.0
    replan_count: float = 0.0
    semantic_state_accuracy: float = 0.0
    objective_count_accuracy: float = 0.0
    temporal_resolution_accuracy: float = 0.0
    unsafe_action_rate: float = 0.0
    p50_latency: float = 0.0
    p95_latency: float = 0.0
    avg_llm_calls: float = 0.0


__all__ = [
    "AgentEvaluationMetrics",
    "CategoryMetrics",
    "EvalCase",
    "EvalCheck",
    "EvalResult",
    "EvaluationReport",
    "EvaluationTrace",
    "FailureCategory",
]
