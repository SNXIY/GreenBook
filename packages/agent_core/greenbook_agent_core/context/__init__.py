"""Canonical working-set Context Runtime.

``SessionContext`` remains the authenticated conversation binding.  The
``ContextSnapshot`` built by this package is a bounded projection of the
durable facts that an Agent decision may consume.  It is deliberately not a
database and it does not replace Task, Execution, Artifact, or Memory
repositories.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .builder import ContextBuilder
from .models import ContextBudget, ContextSnapshot


class RecentEntity(BaseModel):
    ref: str
    kind: str
    entity_id: str
    label: str | None = None
    status: str | None = None
    run_id: str | None = None
    timestamp: datetime | None = None


class RecentToolCall(BaseModel):
    tool_name: str
    tool_call_id: str
    run_id: str
    status: str
    timestamp: datetime | None = None


class PendingApproval(BaseModel):
    approval_id: str
    operation: str
    resource_id: str | None = None
    description: str


class SessionContext(BaseModel):
    """Authenticated conversation scope and explicit active bindings."""

    conversation_id: str
    user_id: str = Field(frozen=True)
    tenant_id: str = Field(frozen=True)
    timezone: str = "Asia/Shanghai"
    active_task_id: str | None = None
    active_artifact_id: str | None = None
    active_draft_id: str | None = None
    active_post_id: str | None = None
    active_schedule_id: str | None = None
    active_execution_id: str | None = None
    recent_entities: list[RecentEntity] = Field(default_factory=list)
    recent_tool_calls: list[RecentToolCall] = Field(default_factory=list)
    pending_approval: PendingApproval | None = None
    conversation_summary: str | None = None
    last_successful_run_id: str | None = None

    def set_active_draft(self, draft_id: str | None) -> None:
        self.active_draft_id = draft_id

    def set_active_post(self, post_id: str | None) -> None:
        self.active_post_id = post_id

    def set_active_schedule(self, schedule_id: str | None) -> None:
        self.active_schedule_id = schedule_id

    def resolve_active_draft_id(self) -> tuple[str | None, list[str]]:
        if self.active_draft_id:
            return self.active_draft_id, []
        drafts = sorted(
            [item for item in self.recent_entities if item.kind == "DRAFT"],
            key=lambda item: item.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if len(drafts) == 1:
            return drafts[0].entity_id, []
        return None, [item.entity_id for item in drafts]

    def resolve_active_schedule_id(self) -> tuple[str | None, list[str]]:
        if self.active_schedule_id:
            return self.active_schedule_id, []
        schedules = sorted(
            [item for item in self.recent_entities if item.kind == "SCHEDULE"],
            key=lambda item: item.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if len(schedules) == 1:
            return schedules[0].entity_id, []
        return None, [item.entity_id for item in schedules]

    def record_entity(
        self,
        ref: str,
        kind: str,
        entity_id: str,
        label: str | None = None,
        status: str | None = None,
        run_id: str | None = None,
    ) -> None:
        entity = RecentEntity(
            ref=ref,
            kind=kind,
            entity_id=entity_id,
            label=label,
            status=status,
            run_id=run_id,
            timestamp=datetime.now(UTC),
        )
        self.recent_entities = [
            item for item in self.recent_entities
            if not (item.entity_id == entity_id and item.kind == kind)
        ][-19:] + [entity]

    def record_tool_call(
        self, tool_name: str, tool_call_id: str, run_id: str, status: str,
    ) -> None:
        self.recent_tool_calls = (
            self.recent_tool_calls + [
                RecentToolCall(
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    run_id=run_id,
                    status=status,
                    timestamp=datetime.now(UTC),
                )
            ]
        )[-20:]

__all__ = [
    "ContextBudget",
    "ContextBuilder",
    "ContextSnapshot",
    "PendingApproval",
    "RecentEntity",
    "RecentToolCall",
    "SessionContext",
]
