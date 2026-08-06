"""Unit tests for assistant core components."""
from __future__ import annotations

from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.memory import ConversationMemory
from greenbook_contracts.identity import AuthContext


class TestSessionContext:
    def test_new_session(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        assert ctx.conversation_id == "conv-1"
        assert ctx.user_id == "u1"
        assert ctx.tenant_id == "t1"
        assert ctx.active_draft_id is None
        assert ctx.active_post_id is None

    def test_set_active_draft(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        ctx.set_active_draft("draft-1")
        assert ctx.active_draft_id == "draft-1"
        assert len(ctx.recent_entities) == 1
        assert ctx.recent_entities[0]["type"] == "DRAFT"

    def test_set_active_post(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        ctx.set_active_post("post-1")
        assert ctx.active_post_id == "post-1"

    def test_set_active_schedule(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        ctx.set_active_schedule("sch-1")
        assert ctx.active_schedule_id == "sch-1"

    def test_record_tool_call(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        ctx.record_tool_call("content_create_draft", {"ok": True, "data": {"draft_id": "d-1"}})
        assert len(ctx.last_successful_tool_calls) == 1
        assert ctx.last_successful_tool_calls[0]["tool"] == "content_create_draft"

    def test_recent_entities_capped(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        for i in range(30):
            ctx.set_active_draft(f"draft-{i}")
        assert len(ctx.recent_entities) <= 30

    def test_snapshot(self) -> None:
        auth = AuthContext(user_id="u1", tenant_id="t1")
        ctx = SessionContext("conv-1", auth)
        ctx.set_active_draft("d-1")
        s = ctx.snapshot()
        assert s["conversation_id"] == "conv-1"
        assert s["active_draft_id"] == "d-1"
        assert "user_id" not in s  # NEVER in snapshot


class TestConversationMemory:
    async def test_load_empty_history(self) -> None:
        mem = ConversationMemory()
        history = await mem.load_history("conv-new")
        assert history == []

    async def test_save_turn_noop(self) -> None:
        mem = ConversationMemory()
        await mem.save_turn("conv-1", "hello", "hi there")
        # Should not raise
