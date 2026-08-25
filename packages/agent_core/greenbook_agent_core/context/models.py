"""Typed, bounded working-set contracts for Agent decisions."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field


class ContextBudget(BaseModel):
    """Limits applied by ContextBuilder before model-facing projection."""

    recent_message_limit: int = Field(default=12, ge=1, le=100)
    recent_message_chars: int = Field(default=12000, ge=500)
    summary_chars: int = Field(default=4000, ge=0)
    max_tasks: int = Field(default=20, ge=1)
    max_goals: int = Field(default=40, ge=1)
    max_operations: int = Field(default=20, ge=1)
    max_artifacts: int = Field(default=40, ge=1)
    max_resources: int = Field(default=50, ge=1)
    max_memories: int = Field(default=8, ge=0)
    max_target_candidates: int = Field(default=80, ge=1)
    max_memory_chars: int = Field(default=1200, ge=200)
    max_operation_chars: int = Field(default=1200, ge=200)
    max_verified_outcomes: int = Field(default=8, ge=0, le=40)


class DerivedConversationContext(BaseModel):
    """Bounded, per-turn projection shared by semantic consumers.

    This is deliberately not a repository model.  Every identity in this
    projection is copied from the canonical Task/Objective/Resource/Execution
    projections, and the projection is discarded after the turn.  The
    canonical view is used by deterministic resolution; the provider-facing
    view is sanitized before it leaves the process.
    """

    relevant_resources: list[dict[str, Any]] = Field(default_factory=list)
    relevant_objectives: list[dict[str, Any]] = Field(default_factory=list)
    recent_verified_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    reference_evidence: list[dict[str, Any]] = Field(default_factory=list)


class ContextSnapshot(BaseModel):
    """Bounded projection of current facts and selected long-term memory.

    Repositories remain the source of truth.  This snapshot is immutable by
    convention for one decision turn; a new snapshot is built after state
    changes instead of growing AgentState indefinitely.
    """

    snapshot_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str = ""
    user_id: str = ""
    tenant_id: str = ""
    timezone: str = "Asia/Shanghai"
    active_task_id: str | None = None
    active_artifact_id: str | None = None
    active_draft_id: str | None = None
    active_post_id: str | None = None
    active_schedule_id: str | None = None
    active_execution_id: str | None = None
    current_command: dict[str, Any] = Field(default_factory=dict)
    current_goal: dict[str, Any] = Field(default_factory=dict)
    recent_messages: list[dict[str, Any]] = Field(default_factory=list)
    summary: str | None = None
    active_tasks: list[dict[str, Any]] = Field(default_factory=list)
    unfinished_goals: list[dict[str, Any]] = Field(default_factory=list)
    task_states: list[dict[str, Any]] = Field(default_factory=list)
    recent_operations: list[dict[str, Any]] = Field(default_factory=list)
    # Bounded receipts read from the existing ActionObservationStore.  This is
    # a projection input, not a second execution/result source of truth.
    recent_verified_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    execution_states: list[dict[str, Any]] = Field(default_factory=list)
    available_resources: list[dict[str, Any]] = Field(default_factory=list)
    target_candidates: list[dict[str, Any]] = Field(default_factory=list)
    user_preferences: list[dict[str, Any]] = Field(default_factory=list)
    recalled_memories: list[dict[str, Any]] = Field(default_factory=list)
    memory_ids_used: list[str] = Field(default_factory=list)
    tool_candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected_tool: str | None = None
    plan_version: int = 0

    def history_for_model(self) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        if self.summary:
            history.append({
                "role": "system",
                "content": f"Conversation summary:\n{self.summary}",
            })
        history.extend(
            {
                "role": str(item.get("role", "")),
                "content": str(item.get("content", "")),
            }
            for item in self.recent_messages
            if item.get("role") in {"user", "assistant"}
        )
        return history

    def decision_payload(self) -> dict[str, Any]:
        """Return the bounded JSON projection shared by Command/Goal/Agent."""

        return self.model_dump(mode="json", exclude={"summary"}) | {
            "conversation_summary": self.summary,
            "history": self.history_for_model(),
        }

    def target_payload(self) -> list[dict[str, Any]]:
        """Return structured candidates without selecting one implicitly."""

        return list(self.target_candidates or self.available_resources)


__all__ = [
    "ContextBudget",
    "ContextSnapshot",
    "DerivedConversationContext",
]
