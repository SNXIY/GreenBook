"""RuntimeContext — complete execution context for one Agent invocation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from greenbook_assistant_core.task.models import ArtifactRef, ResolvedTaskTarget


@dataclass(frozen=True)
class TargetContext:
    """Small, resolved reference to the business object being operated on."""

    task_id: str
    artifact_id: str | None = None
    resource_id: str | None = None
    resource_kind: str | None = None


@dataclass(frozen=True)
class TaskContext:
    """Resolved Assistant business context passed into Runtime."""

    task_id: str
    goal: str
    task_intent: Any
    target: TargetContext | ResolvedTaskTarget | None = None
    constraints: tuple[dict[str, Any], ...] = ()
    active_artifact_id: str | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        """Normalize mutable inputs into a detached execution snapshot."""
        refs_list: list[ArtifactRef] = []
        for raw_ref in self.artifact_refs:
            if isinstance(raw_ref, ArtifactRef):
                refs_list.append(raw_ref)
                continue
            ref_data = dict(raw_ref)
            # Runtime result projections created before the typed boundary may
            # omit task_id.  Fill only that legacy omission; an explicit
            # mismatching task_id remains invalid at compiler validation.
            ref_data.setdefault("task_id", self.task_id)
            refs_list.append(ArtifactRef.model_validate(ref_data))
        refs = tuple(refs_list)
        constraints = tuple(deepcopy(item) for item in self.constraints)
        object.__setattr__(self, "artifact_refs", refs)
        object.__setattr__(self, "constraints", constraints)
        if hasattr(self.task_intent, "model_copy"):
            object.__setattr__(
                self,
                "task_intent",
                self.task_intent.model_copy(deep=True),
            )


@dataclass
class RuntimeContext:
    """Self-contained context for one Agent execution.

    Built by routes.py from the HTTP request and passed to the Service
    layer.  Services never access Request/HTTP objects directly.
    """

    # ── identifiers ──
    conversation_id: str = ""
    run_id: str = ""
    trace_id: str = ""
    task_id: str = ""            # resolved target task (Phase 2.5)
    execution_id: str = ""       # assigned by Worker (Phase 3+)
    task_context: TaskContext | None = None

    # ── user ──
    user_id: str = ""
    tenant_id: str = ""
    timezone: str = "Asia/Shanghai"

    # ── input ──
    user_message: str = ""
    conversation_history: list[dict[str, str]] = field(default_factory=list)

    # ── understanding result ──
    task_intent: Any = None      # TaskIntent | None

    # ── session snapshot ──
    session: Any = None          # SessionContext
    active_artifact_id: str | None = None
    active_draft_id: str | None = None
    active_schedule_id: str | None = None

    # ── resource resolution — Phase 5.6 ──
    resolved_resources: Any = None  # ResourceResolutionResult | None
    recent_tasks: Any = None        # legacy compatibility input; not resolved in Runtime

    # ── agent memory — Phase 6.6 ──
    memory_context: dict[str, Any] = field(default_factory=dict)
    # {preferences: [{type, value, confidence}], recent_tasks: [{goal, status}], ...}

    # ── injected dependencies ──
    mcp: Any = None              # GreenBookMCPServer
    llm: Any = None              # AsyncOpenAI
    model: str = ""
    auth: Any = None             # AuthContext (for MCP tool calls)
