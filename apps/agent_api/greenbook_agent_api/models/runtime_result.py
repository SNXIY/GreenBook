"""RuntimeResult — unified result from any Agent execution path."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeResult:
    """Single return type for both Legacy and Runtime execution paths.

    routes.py maps this to HTTP responses without knowing which path
    produced it.
    """

    # ── status ──
    success: bool = False
    status: str = ""             # COMPLETED | FAILED | WAITING_APPROVAL | PARTIAL_FAILURE
    run_id: str = ""
    task_id: str = ""
    plan_id: str = ""
    execution_id: str | None = None

    # ── user-visible ──
    content: str = ""
    summary: str = ""

    # ── execution metadata ──
    started_execution: bool = False
    side_effect_committed: bool = False
    fallback_allowed: bool = True
    execution_path: str = ""     # "legacy" | "runtime"

    # ── error ──
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False

    # ── SSE events ──
    events: list[dict[str, Any]] = field(default_factory=list)

    # ── produced resources ──
    draft_id: str | None = None
    schedule_id: str | None = None
    artifact_ids: list[str] = field(default_factory=list)
    # Result payloads consumed by the presentation layer.  This is not
    # execution state and does not change PlanExecution or its lifecycle.
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    # Optional read-model projections.  Runtime implementations may populate
    # these when a caller needs a single result envelope; they are not a
    # second source of execution truth.
    steps: list[dict[str, Any]] = field(default_factory=list)
    schedule: dict[str, Any] | None = None

    # ── trace ──
    trace_id: str = ""
    tool_rounds: int = 0
    duration_ms: float = 0.0
    approval_id: str | None = None
    approval_data: dict[str, Any] | None = None  # for persistence
    approval: dict[str, Any] | None = None
    session_snapshot: dict[str, Any] | None = None
    partial_results: dict[str, Any] | None = None
    failure_state: dict[str, Any] | None = None  # for HTTP error detail
    error: str | None = None
    # Serialised AgentResponse, attached after Runtime execution so HTTP
    # and direct service callers share one presentation contract.
    presentation: dict[str, Any] | None = None
