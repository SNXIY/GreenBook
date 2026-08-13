"""Phase17 durable Conversation/Message/Context runtime tests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.conversation.service import ConversationService


def test_phase17_migration_is_one_asyncpg_prepared_statement() -> None:
    migration = (
        Path(__file__).parents[2]
        / "packages"
        / "agent_core"
        / "greenbook_agent_core"
        / "db"
        / "migrations"
        / "003_conversation_runtime_context.sql"
    )
    sql = "\n".join(
        line for line in migration.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    ).strip()

    assert sql.count(";") == 1
    assert sql.endswith(";")


class _Store:
    def __init__(self) -> None:
        self.conversations: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}


class _Session:
    def __init__(self, store: _Store) -> None:
        self.store = store


class _ConversationRepository:
    def __init__(self, session: _Session) -> None:
        self.store = session.store

    async def ensure_tables(self) -> None:
        return None

    async def create(self, conversation_id, user_id, tenant_id, title=None, timezone="Asia/Shanghai"):
        record = {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "title": title,
            "timezone": timezone,
            "active_task_id": None,
            "active_artifact_id": None,
            "active_draft_id": None,
            "active_schedule_id": None,
            "active_post_id": None,
            "recent_entities": [],
            "recent_tool_calls": [],
            "pending_approval": None,
            "last_successful_run_id": None,
            "conversation_summary": None,
            "version": 1,
        }
        self.store.conversations[conversation_id] = record
        return dict(record)

    async def find_by_id(self, conversation_id):
        record = self.store.conversations.get(conversation_id)
        return dict(record) if record else None

    async def find_all_by_user(self, user_id, tenant_id):
        return [
            dict(record)
            for record in self.store.conversations.values()
            if record["user_id"] == user_id and record["tenant_id"] == tenant_id
        ]

    async def update(self, conversation_id, **fields):
        record = self.store.conversations.get(conversation_id)
        if record is None:
            return None
        record.update(fields)
        record["version"] = record.get("version", 1) + 1
        return dict(record)


class _MessageRepository:
    def __init__(self, session: _Session) -> None:
        self.store = session.store

    async def add(self, conversation_id, role, content, trace_id=None):
        self.store.messages.setdefault(conversation_id, []).append({
            "role": role,
            "content": content,
            "trace_id": trace_id,
        })

    async def find_by_conversation(self, conversation_id, *, roles=None, limit=None):
        messages = list(self.store.messages.get(conversation_id, []))
        if roles:
            messages = [item for item in messages if item["role"] in roles]
        if limit is not None:
            messages = messages[-limit:]
        return messages


class _ContextRepository:
    def __init__(self, session: _Session) -> None:
        self.store = session.store

    async def update(self, conversation_id, **fields):
        record = self.store.conversations.get(conversation_id)
        if record is None:
            return None
        record.update(fields)
        record["version"] = record.get("version", 1) + 1
        return dict(record)


@pytest.fixture
def manager(monkeypatch):
    store = _Store()

    @asynccontextmanager
    async def sessions():
        yield _Session(store)

    monkeypatch.setattr(
        "greenbook_agent_core.conversation.service.ConversationRepository",
        _ConversationRepository,
    )
    monkeypatch.setattr(
        "greenbook_agent_core.conversation.service.MessageRepository",
        _MessageRepository,
    )
    monkeypatch.setattr(
        "greenbook_agent_core.conversation.service.ContextRepository",
        _ContextRepository,
    )
    return store, ConversationService(
        session_context_factory=sessions,
        recent_message_limit=2,
        compression_threshold=3,
    )


@pytest.mark.asyncio
async def test_multi_day_conversation_recovers_context_task_and_artifact(manager) -> None:
    store, day_one = manager
    conversation_id = "conversation-phase17"
    await day_one.create_conversation(
        conversation_id=conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
        title="Java 学习帖子",
    )
    await day_one.append_message(
        conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
        role="user",
        content="帮我生成Java学习帖子",
    )
    await day_one.append_message(
        conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
        role="assistant",
        content="已生成 Java 学习帖子草稿。",
    )
    session = SessionContext(
        conversation_id=conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
        active_task_id="task-day-one",
        active_artifact_id="artifact-java-draft",
        active_draft_id="draft-java-1",
    )
    await day_one.save_session(session)

    # A new manager models a new API process. It sees the same durable store.
    day_two = ConversationService(
        session_context_factory=day_one._session_context_factory,
        recent_message_limit=2,
        compression_threshold=3,
    )
    restored = await day_two.load(
        conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    assert restored.session.active_task_id == "task-day-one"
    assert restored.session.active_artifact_id == "artifact-java-draft"
    assert restored.session.active_draft_id == "draft-java-1"
    assert restored.recent_messages[-1]["content"] == "已生成 Java 学习帖子草稿。"

    await day_two.append_message(
        conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
        role="user",
        content="修改昨天那个帖子，周五发布",
    )
    next_turn = await day_two.load(
        conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    assert "昨天那个帖子" in next_turn.history_for_model()[-1]["content"]
    assert store.conversations[conversation_id]["active_artifact_id"] == "artifact-java-draft"


@pytest.mark.asyncio
async def test_context_compression_persists_summary_and_keeps_recent_messages(manager) -> None:
    store, context = manager
    conversation_id = "conversation-compression"
    await context.create_conversation(
        conversation_id=conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    for role, content in [
        ("user", "第一轮"),
        ("assistant", "第一轮答复"),
        ("user", "第二轮"),
    ]:
        await context.append_message(
            conversation_id,
            user_id="user-1",
            tenant_id="tenant-1",
            role=role,
            content=content,
        )

    restored = await context.load(
        conversation_id,
        user_id="user-1",
        tenant_id="tenant-1",
    )
    assert restored.summary is not None
    assert "第一轮" in restored.summary
    assert [item["content"] for item in restored.recent_messages] == ["第一轮答复", "第二轮"]
    assert store.conversations[conversation_id]["conversation_summary"] == restored.summary


@pytest.mark.asyncio
async def test_context_scope_isolation_survives_process_restart(manager) -> None:
    _, context = manager
    await context.create_conversation(
        conversation_id="conversation-private",
        user_id="user-1",
        tenant_id="tenant-1",
    )
    with pytest.raises(LookupError):
        await context.load(
            "conversation-private",
            user_id="user-2",
            tenant_id="tenant-1",
        )
