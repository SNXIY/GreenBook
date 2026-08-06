"""Lightweight SessionContext — user_id and tenant_id only from AuthContext, never model-overridable."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class RecentEntity(BaseModel):
    ref: str
    kind: str  # DRAFT, POST, SCHEDULE, ARTIFACT
    entity_id: str
    label: str | None = None
    status: str | None = None
    run_id: str | None = None
    timestamp: datetime | None = None


class RecentToolCall(BaseModel):
    tool_name: str
    tool_call_id: str
    run_id: str
    status: str  # SUCCESS, FAILED
    timestamp: datetime | None = None


class PendingApproval(BaseModel):
    approval_id: str
    operation: str
    resource_id: str | None = None
    description: str


class SessionContext(BaseModel):
    conversation_id: str
    user_id: str = Field(frozen=True)
    tenant_id: str = Field(frozen=True)
    timezone: str = "Asia/Shanghai"
    active_draft_id: str | None = None
    active_post_id: str | None = None
    active_schedule_id: str | None = None
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
        """Resolve "the draft I just mentioned" in priority order.

        Returns (resolved_id, candidate_ids_when_ambiguous).
        """
        # 1. Explicit active_draft_id
        if self.active_draft_id:
            return self.active_draft_id, []

        # 2. Most recent successful Draft entity
        drafts = sorted(
            [e for e in self.recent_entities if e.kind == "DRAFT"],
            key=lambda e: e.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if len(drafts) == 1:
            return drafts[0].entity_id, []
        if len(drafts) > 1:
            return None, [d.entity_id for d in drafts]

        return None, []

    def resolve_active_schedule_id(self) -> tuple[str | None, list[str]]:
        """Resolve "the schedule I just mentioned" in priority order."""
        # 1. Explicit active_schedule_id
        if self.active_schedule_id:
            return self.active_schedule_id, []

        # 2. Most recent successful publication.schedule
        schedules = sorted(
            [e for e in self.recent_entities if e.kind == "SCHEDULE"],
            key=lambda e: e.timestamp or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        if len(schedules) == 1:
            return schedules[0].entity_id, []
        if len(schedules) > 1:
            return None, [s.entity_id for s in schedules]

        return None, []

    def record_entity(
        self, ref: str, kind: str, entity_id: str, label: str | None = None,
        status: str | None = None, run_id: str | None = None,
    ) -> None:
        entity = RecentEntity(
            ref=ref, kind=kind, entity_id=entity_id, label=label,
            status=status, run_id=run_id, timestamp=datetime.now(UTC),
        )
        self.recent_entities = [
            e for e in self.recent_entities
            if not (e.entity_id == entity_id and e.kind == kind)
        ]
        self.recent_entities.append(entity)
        if len(self.recent_entities) > 20:
            self.recent_entities = self.recent_entities[-20:]

    def record_tool_call(
        self, tool_name: str, tool_call_id: str, run_id: str, status: str,
    ) -> None:
        call = RecentToolCall(
            tool_name=tool_name, tool_call_id=tool_call_id,
            run_id=run_id, status=status,
            timestamp=datetime.now(UTC),
        )
        self.recent_tool_calls.append(call)
        if len(self.recent_tool_calls) > 20:
            self.recent_tool_calls = self.recent_tool_calls[-20:]
