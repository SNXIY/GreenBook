"""Unit tests for assistant core — SessionContext and ConversationMemory."""

from __future__ import annotations

from greenbook_assistant_core.context import SessionContext
from greenbook_assistant_core.memory import ConversationMemory


class TestSessionContext:
    def test_new_session(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        assert ctx.conversation_id == "conv-1"
        assert ctx.user_id == "u1"
        assert ctx.tenant_id == "t1"
        assert ctx.active_draft_id is None
        assert ctx.active_post_id is None

    def test_set_active_draft(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        ctx.active_draft_id = "draft-1"
        ctx.record_entity(ref="draft:draft-1", kind="DRAFT", entity_id="draft-1", label="Test", status="READY")
        assert ctx.active_draft_id == "draft-1"
        assert len(ctx.recent_entities) == 1
        assert ctx.recent_entities[0].kind == "DRAFT"

    def test_set_active_post(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        ctx.active_post_id = "post-1"
        assert ctx.active_post_id == "post-1"

    def test_set_active_schedule(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        ctx.active_schedule_id = "sch-1"
        assert ctx.active_schedule_id == "sch-1"

    def test_record_tool_call(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        ctx.record_tool_call("content.create_draft", "tc-1", "run-1", "SUCCESS")
        assert len(ctx.recent_tool_calls) == 1
        assert ctx.recent_tool_calls[0].tool_name == "content.create_draft"

    def test_recent_entities_capped(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        for i in range(30):
            ctx.record_entity(
                ref=f"draft:draft-{i}", kind="DRAFT", entity_id=f"draft-{i}",
                label=f"Draft {i}", status="READY",
            )
        assert len(ctx.recent_entities) <= 20

    def test_snapshot(self) -> None:
        ctx = SessionContext(conversation_id="conv-1", user_id="u1", tenant_id="t1")
        ctx.active_draft_id = "d-1"
        d = ctx.model_dump(mode="json")
        assert d["conversation_id"] == "conv-1"
        assert d["active_draft_id"] == "d-1"
        assert d["user_id"] == "u1"
        assert "raw_access_token" not in d


class TestConversationMemory:
    async def test_load_empty_history(self) -> None:
        mem = ConversationMemory()
        history = await mem.load_history("conv-new")
        assert history == []

    async def test_save_turn_noop(self) -> None:
        mem = ConversationMemory()
        await mem.save_turn("conv-1", "hello", "hi there")
