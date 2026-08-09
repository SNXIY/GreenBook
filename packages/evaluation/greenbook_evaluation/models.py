"""Evaluation models — EvalCase, EvalResult, EvaluationReport."""

from __future__ import annotations

from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    """One evaluation test case."""

    case_id: str
    category: str               # INTENT | DECOMPOSITION | REFERENCE | RESOURCE | PLAN | EXECUTION
    description: str = ""       # human-readable
    user_message: str = ""

    # ── conversation context (tasks + artifacts from prior rounds) ──
    existing_tasks: list[dict] = []
    # [{task_id, goal, goal_category, created_at_ago: int,
    #   artifacts: [{type, resource_id}]}]

    # ── expected outputs ──
    expected_intent: dict | None = None
    # {goal_category, relation, requirements: [{type}],
    #  resource_requests: [{operation, resource_type}]}

    expected_sub_task_count: int | None = None
    expected_template: str | None = None
    expected_tools: list[str] | None = None          # tool names called
    expected_resource_id: str | None = None           # draft_id or schedule_id
    expected_reference_task_id: str | None = None

    # ── expected outcome ──
    should_succeed: bool = True
    expected_status: str = "COMPLETED"               # COMPLETED | FAILED | WAITING_APPROVAL | PARTIAL | SKIPPED
    expected_clarification: bool = False             # True when needs_clarification
    expected_trace_events: list[str] | None = None   # [TASK_CREATED, TOOL_INVOKED, …]


class EvalCheck(BaseModel):
    """One individual check within an EvalResult."""
    check: str = ""              # "intent.goal_category" | "template" | "tool"
    expected: object = None
    actual: object = None
    ok: bool = False


class EvalResult(BaseModel):
    """Result of running one EvalCase."""
    case_id: str = ""
    category: str = ""
    description: str = ""
    passed: bool = False
    checks: list[EvalCheck] = []
    errors: list[str] = []
    duration_ms: float = 0.0
    trace_summary: dict = Field(default_factory=dict)
    # {event_count, tool_count, step_count}


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
