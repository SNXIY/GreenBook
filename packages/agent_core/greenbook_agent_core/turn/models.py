"""Typed contracts for the Phase 3A unified Turn boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..command.models import CommandContext
from ..context.models import (
    ContextBudget,
    ContextSnapshot,
    DerivedConversationContext,
)


class TurnRoute(StrEnum):
    """Routing decision produced by FastPathGate for one user turn."""

    FAST = "FAST"          # single explicit write, no Agent reasoning
    QUERY = "QUERY"        # single read
    CHAT = "CHAT"          # plain conversation, no execution is warranted
    CLARIFY = "CLARIFY"    # underspecified / ambiguous / missing parameters
    COMPLEX = "COMPLEX"    # Objective-driven ActionLoop path


class TurnBudget(BaseModel):
    """Simple context budget applied by ContextAssembler's Fast Path trimming."""

    recent_message_limit: int = Field(default=10, ge=1, le=100)
    recent_message_chars: int = Field(default=6000, ge=500)
    summary_chars: int = Field(default=2000, ge=0)
    max_focus_tasks: int = Field(default=6, ge=1)
    max_objectives: int = Field(default=12, ge=0)
    max_scoped_artifacts: int = Field(default=20, ge=0)
    max_scoped_resources: int = Field(default=30, ge=0)
    max_scoped_executions: int = Field(default=10, ge=0)
    artifact_max_chars: int = Field(default=1500, ge=200)
    max_completed_objective_summary: int = Field(default=4, ge=0)
    max_memories: int = Field(default=5, ge=0, le=5)
    max_memory_chars: int = Field(default=1200, ge=200)
    max_verified_outcomes: int = Field(default=8, ge=0)


class FastPathDecision(BaseModel):
    """Outcome of FastPathGate for one Command."""

    model_config = ConfigDict(extra="forbid")

    route: TurnRoute
    # Canonical semantic actions extracted from the structured Command /
    # TaskDelta, never from keyword rules over the user text.
    semantic_actions: list[str] = Field(default_factory=list)
    capability: str = ""
    tool_name: str = ""
    reason: str = ""


class AssembledTurnContext(BaseModel):
    """Bounded working set for one turn.

    ``snapshot`` is the canonical ContextSnapshot reused by the Command
    interpreter and TargetResolver.  The ``selected_*`` views are task-scoped
    trims used by the Fast Path so one Task's artifacts/resources never leak
    into another Task's turn.  ``focus_task_ids`` is the conversation focus,
    never a single implicit "active" task by itself.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    conversation_id: str
    user_id: str
    tenant_id: str
    timezone: str
    snapshot: ContextSnapshot
    derived_context: DerivedConversationContext = Field(
        default_factory=DerivedConversationContext
    )
    focus_task_ids: list[str] = Field(default_factory=list)
    selected_tasks: list[dict[str, Any]] = Field(default_factory=list)
    selected_objectives: list[dict[str, Any]] = Field(default_factory=list)
    selected_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    selected_resources: list[dict[str, Any]] = Field(default_factory=list)
    selected_executions: list[dict[str, Any]] = Field(default_factory=list)
    budget: TurnBudget = Field(default_factory=TurnBudget)

    @property
    def conversation_id_safe(self) -> str:
        return self.conversation_id

    def to_command_context(self) -> CommandContext:
        """Share one scoped projection, with separate ID visibility rules."""

        base = CommandContext.from_any(self.snapshot)
        selected_tasks = list(self.selected_tasks or base.active_tasks)
        selected_objectives = list(self.selected_objectives or base.unfinished_goals)
        selected_resources = [
            dict(item)
            for item in (self.selected_resources or [])
            if str(item.get("lifecycle") or "").upper() == "CURRENT"
        ]
        # Tasks remain in the canonical active_tasks view.  Top-level targets
        # are resource candidates only; duplicating Task cards here makes the
        # provider confuse ownership containers with writable resources.
        targets = selected_resources
        # An explicit reference with no scoped candidates must stay empty so
        # the resolver can return NOT_FOUND/clarification. Falling back to
        # the full snapshot here would reintroduce context contamination.
        if not targets and not self.derived_context.reference_evidence:
            targets = list(base.targets)
        metadata = dict(base.metadata or {})
        metadata["derived_context"] = self.derived_context.model_dump(mode="json")
        return CommandContext(
            conversation_id=self.conversation_id or base.conversation_id,
            timezone=self.timezone or base.timezone,
            active_target=base.active_target,
            active_tasks=selected_tasks,
            unfinished_goals=selected_objectives,
            targets=targets,
            history=list(base.history),
            summary=base.summary,
            reference_evidence=list(self.derived_context.reference_evidence),
            recent_verified_outcomes=list(
                self.derived_context.recent_verified_outcomes
            ),
            metadata=metadata,
        )


class TurnRequest(BaseModel):
    """Bounded input to TurnCoordinator.execute for one user turn."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    conversation_id: str
    user_id: str
    tenant_id: str
    message: str
    history: list[dict[str, Any]] | None = None
    timezone: str = "Asia/Shanghai"
    session: Any = None
    run_id: str = ""
    trace_id: str = ""
    focus_task_ids: list[str] = Field(default_factory=list)
    current_command: Any = None
    llm: Any = None
    model: str = ""
    auth: Any = None
    mcp: Any = None
    idempotency_key: str = ""
    activity_callback: Any = None
    completion_callback: Any = None


__all__ = [
    "AssembledTurnContext",
    "ContextBudget",
    "FastPathDecision",
    "TurnBudget",
    "TurnRequest",
    "TurnRoute",
]
