"""Long-lived Conversation context and short/long memory policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any

from greenbook_agent_core.context import SessionContext
from greenbook_agent_core.db.connection import session_ctx
from greenbook_agent_core.db.migration_runner import apply_migrations
from greenbook_agent_core.db.repositories import (
    ContextRepository,
    ConversationRepository,
    MessageRepository,
)


class ConversationNotFoundError(LookupError):
    """Raised when a conversation is absent or outside the authenticated scope."""


@dataclass(frozen=True, slots=True)
class ConversationContextSnapshot:
    """Bounded durable facts used to decide one Runtime turn.

    ConversationService owns only the conversation fields.  The API composition
    boundary enriches the remaining projections from Task/Artifact/Execution
    repositories before command and target resolution.
    """

    session: SessionContext
    recent_messages: list[dict[str, Any]]
    summary: str | None = None
    active_tasks: list[dict[str, Any]] = field(default_factory=list)
    unfinished_goals: list[dict[str, Any]] = field(default_factory=list)
    recent_operations: list[dict[str, Any]] = field(default_factory=list)
    available_resources: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    user_preferences: list[dict[str, Any]] = field(default_factory=list)

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
        """Return bounded, JSON-safe context for semantic providers."""

        return {
            "conversation_id": self.session.conversation_id,
            "timezone": self.session.timezone,
            "conversation_summary": self.summary,
            "active_tasks": list(self.active_tasks),
            "unfinished_goals": list(self.unfinished_goals),
            "recent_operations": list(self.recent_operations),
            "available_resources": list(self.available_resources),
            "user_preferences": list(self.user_preferences),
        }


class ConversationService:
    """Own durable conversation state without owning Task or Execution state."""

    def __init__(
        self,
        *,
        session_context_factory: Callable[[], Any] | None = None,
        recent_message_limit: int = 12,
        compression_threshold: int = 24,
        summary_builder: Callable[[str, list[dict[str, Any]]], str | Awaitable[str]]
        | None = None,
    ) -> None:
        self._session_context_factory = session_context_factory or session_ctx
        self._recent_limit = max(1, recent_message_limit)
        self._compression_threshold = max(self._recent_limit + 1, compression_threshold)
        self._summary_builder = summary_builder

    async def ensure_storage(self) -> None:
        """Apply migrations and create the Conversation persistence tables."""

        async with self._session_context_factory() as session:
            await ConversationRepository(session).ensure_tables()
            await apply_migrations(session)

    async def create_conversation(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        title: str | None = None,
        timezone: str = "Asia/Shanghai",
    ) -> dict[str, Any]:
        async with self._session_context_factory() as session:
            return await ConversationRepository(session).create(
                conversation_id,
                user_id,
                tenant_id,
                title=title,
                timezone=timezone,
            )

    async def get_conversation(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> dict[str, Any] | None:
        async with self._session_context_factory() as session:
            conversation = await ConversationRepository(session).find_by_id(conversation_id)
        if conversation is None or not _belongs_to(conversation, user_id, tenant_id):
            return None
        return conversation

    async def list_conversations(
        self,
        *,
        user_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        async with self._session_context_factory() as session:
            return await ConversationRepository(session).find_all_by_user(user_id, tenant_id)

    async def load(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> ConversationContextSnapshot:
        async with self._session_context_factory() as session:
            conversation = await ConversationRepository(session).find_by_id(conversation_id)
            if conversation is None or not _belongs_to(conversation, user_id, tenant_id):
                raise ConversationNotFoundError(conversation_id)
            messages = await MessageRepository(session).find_by_conversation(
                conversation_id,
                roles=("user", "assistant"),
                limit=self._recent_limit,
            )
        return ConversationContextSnapshot(
            session=_session_from_record(conversation),
            recent_messages=messages,
            summary=conversation.get("conversation_summary"),
        )

    async def append_message(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
        role: str,
        content: str,
        trace_id: str | None = None,
        parts: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        execution_id: str | None = None,
    ) -> None:
        conversation = await self.get_conversation(
            conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        async with self._session_context_factory() as session:
            messages = MessageRepository(session)
            message_fields: dict[str, Any] = {"trace_id": trace_id}
            if parts:
                message_fields["parts"] = list(parts)
            if run_id:
                message_fields["run_id"] = run_id
            if execution_id:
                message_fields["execution_id"] = execution_id
            await messages.add(conversation_id, role, content, **message_fields)
            # Count instead of loading the whole history: the append path must
            # stay O(recent) even for long-lived conversations.
            count = await messages.count_by_conversation(conversation_id)
            if count >= self._compression_threshold:
                all_messages = await messages.find_by_conversation(
                    conversation_id,
                    limit=self._compression_threshold,
                )
                await self._compress_in_session(
                    session,
                    conversation_id,
                    conversation.get("conversation_summary"),
                    all_messages,
                )
                # The folded messages now live in the durable summary; trim
                # the raw rows so each message is compacted exactly once and
                # the table stays bounded.
                await messages.trim(conversation_id, keep=self._recent_limit)

    async def list_messages(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> list[dict[str, Any]]:
        conversation = await self.get_conversation(
            conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        async with self._session_context_factory() as session:
            return await MessageRepository(session).find_by_conversation(conversation_id)

    async def update_message_projection(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
        trace_id: str,
        content: str,
        parts: list[dict[str, Any]],
        run_id: str | None = None,
        execution_id: str | None = None,
    ) -> bool:
        conversation = await self.get_conversation(
            conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        async with self._session_context_factory() as session:
            return await MessageRepository(session).update_projection_by_trace(
                conversation_id,
                trace_id=trace_id,
                content=content,
                parts=parts,
                run_id=run_id,
                execution_id=execution_id,
            )

    async def save_session(
        self,
        session_context: SessionContext,
        *,
        title: str | None = None,
    ) -> dict[str, Any] | None:
        values = session_context.model_dump(mode="json")
        fields = {
            "timezone": values.get("timezone", "Asia/Shanghai"),
            "active_task_id": values.get("active_task_id"),
            "active_artifact_id": values.get("active_artifact_id"),
            "active_draft_id": values.get("active_draft_id"),
            "active_schedule_id": values.get("active_schedule_id"),
            "active_post_id": values.get("active_post_id"),
            "recent_entities": values.get("recent_entities", []),
            "recent_tool_calls": values.get("recent_tool_calls", []),
            "pending_approval": values.get("pending_approval"),
            "last_successful_run_id": values.get("last_successful_run_id"),
        }
        if title is not None:
            fields["title"] = title
        async with self._session_context_factory() as session:
            # ContextRepository intentionally rejects title; keep title on the
            # ConversationRepository aggregate when a caller explicitly sets it.
            if title is not None:
                result = await ConversationRepository(session).update(
                    session_context.conversation_id,
                    title=title,
                    **{key: value for key, value in fields.items() if key != "title"},
                )
            else:
                result = await ContextRepository(session).update(
                    session_context.conversation_id,
                    **fields,
                )
        return result

    async def compress(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
        summary: str | None = None,
    ) -> str:
        conversation = await self.get_conversation(
            conversation_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        async with self._session_context_factory() as session:
            messages = MessageRepository(session)
            all_messages = await messages.find_by_conversation(conversation_id)
            value = await self._build_summary(
                summary,
                conversation.get("conversation_summary"),
                all_messages,
            )
            await ContextRepository(session).update(
                conversation_id,
                conversation_summary=value,
            )
            # An explicit full compression also trims the raw rows that were
            # folded into the durable summary.
            await messages.trim(conversation_id, keep=self._recent_limit)
        return value

    async def _compress_in_session(
        self,
        session: Any,
        conversation_id: str,
        existing_summary: str | None,
        messages: list[dict[str, Any]],
    ) -> None:
        value = await self._build_summary(None, existing_summary, messages)
        await ContextRepository(session).update(
            conversation_id,
            conversation_summary=value,
        )

    async def _build_summary(
        self,
        requested: str | None,
        existing: str | None,
        messages: list[dict[str, Any]],
    ) -> str:
        if requested is not None:
            return requested[:6000]
        older = messages[:-self._recent_limit]
        if self._summary_builder is not None:
            rendered = self._summary_builder(existing or "", older)
            if isawaitable(rendered):
                rendered = await rendered
            return str(rendered)[:6000]
        lines = [f"{item.get('role', 'user')}: {item.get('content', '')}" for item in older]
        return _merge_summary(existing, "\n".join(lines))


def _merge_summary(existing: str | None, additions: str, *, limit: int = 6000) -> str:
    """Keep prior durable facts while bounding newly compacted conversation text.

    Compaction can run repeatedly over retained messages (explicit
    ``compress`` calls, or the trigger re-firing before a trim lands), so
    lines already captured in the durable summary are not appended again —
    repeated runs must not duplicate facts (design goal 0813 — the summary
    grows once per fact, not once per compression).
    """

    previous = (existing or "").strip()
    current = additions.strip()
    if not previous:
        return current[:limit]
    if not current:
        return previous[:limit]
    existing_lines = {line.strip() for line in previous.splitlines()}
    fresh = [
        line.strip()
        for line in current.splitlines()
        if line.strip() and line.strip() not in existing_lines
    ]
    if not fresh:
        return previous[:limit]
    joined = "\n".join(fresh)
    available = max(0, limit - len(previous) - 1)
    if available == 0:
        return previous[:limit]
    return f"{previous}\n{joined[:available]}"


def _belongs_to(record: dict[str, Any], user_id: str, tenant_id: str) -> bool:
    return str(record.get("user_id", "")) == user_id and str(record.get("tenant_id", "")) == tenant_id


def _session_from_record(record: dict[str, Any]) -> SessionContext:
    allowed = {
        "conversation_id", "user_id", "tenant_id", "timezone",
        "active_task_id", "active_artifact_id", "active_draft_id",
        "active_schedule_id", "active_post_id", "recent_entities",
        "recent_tool_calls", "pending_approval", "conversation_summary",
        "last_successful_run_id",
    }
    values = {key: record.get(key) for key in allowed}
    values["timezone"] = record.get("timezone") or "Asia/Shanghai"
    values["recent_entities"] = record.get("recent_entities") or []
    values["recent_tool_calls"] = record.get("recent_tool_calls") or []
    return SessionContext.model_validate(values)


__all__ = [
    "ConversationContextSnapshot",
    "ConversationNotFoundError",
    "ConversationService",
]
