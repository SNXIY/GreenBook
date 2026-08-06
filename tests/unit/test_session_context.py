"""Unit tests for SessionContext entity resolution."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from greenbook_assistant_core.context import RecentEntity, SessionContext
from pydantic import ValidationError


def make_session() -> SessionContext:
    return SessionContext(
        conversation_id="conv-1",
        user_id="user-1",
        tenant_id="tenant-1",
        timezone="Asia/Shanghai",
    )


def test_active_draft_resolved_first():
    s = make_session()
    s.active_draft_id = "draft-1"
    s.recent_entities = [
        RecentEntity(
            ref="draft:draft-2", kind="DRAFT", entity_id="draft-2",
            label="Second", timestamp=datetime.now(UTC),
        ),
    ]
    resolved, _ = s.resolve_active_draft_id()
    assert resolved == "draft-1"


def test_single_draft_resolved_from_entities():
    s = make_session()
    s.record_entity(
        ref="draft:draft-1", kind="DRAFT", entity_id="draft-1",
        label="My Draft", status="READY",
    )
    resolved, _ = s.resolve_active_draft_id()
    assert resolved == "draft-1"


def test_ambiguous_drafts_return_candidates():
    s = make_session()
    s.record_entity(ref="draft:a", kind="DRAFT", entity_id="a", label="A")
    s.record_entity(ref="draft:b", kind="DRAFT", entity_id="b", label="B")
    resolved, candidates = s.resolve_active_draft_id()
    assert resolved is None
    assert len(candidates) == 2


def test_no_draft_returns_none():
    s = make_session()
    resolved, candidates = s.resolve_active_draft_id()
    assert resolved is None
    assert candidates == []


def test_schedule_resolution():
    s = make_session()
    s.active_schedule_id = "sched-123"
    resolved, _ = s.resolve_active_schedule_id()
    assert resolved == "sched-123"


def test_single_schedule_from_entities():
    s = make_session()
    s.record_entity(ref="schedule:s1", kind="SCHEDULE", entity_id="s1", status="SCHEDULED")
    resolved, _ = s.resolve_active_schedule_id()
    assert resolved == "s1"


def test_record_tool_call():
    s = make_session()
    s.record_tool_call("content.create_draft", "tc-1", "run-1", "SUCCESS")
    assert len(s.recent_tool_calls) == 1
    assert s.recent_tool_calls[0].tool_name == "content.create_draft"
    assert s.recent_tool_calls[0].status == "SUCCESS"


def test_record_entity_dedup():
    s = make_session()
    s.record_entity(ref="draft:a", kind="DRAFT", entity_id="a", label="First")
    s.record_entity(ref="draft:a", kind="DRAFT", entity_id="a", label="Updated")
    drafts = [e for e in s.recent_entities if e.kind == "DRAFT"]
    assert len(drafts) == 1
    assert drafts[0].label == "Updated"


def test_user_id_frozen():
    s = make_session()
    with pytest.raises(ValidationError):
        s.user_id = "hacked"
