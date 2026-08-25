"""Durable store for public-safe UserActivity events.

The execution event log remains an internal diagnostic log.  This store is a
separate, deliberately small projection table with an integer cursor that can
be used safely by SSE reconnects and polling clients.
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Protocol

import sqlalchemy as sa
from greenbook_contracts.user_activity import UserActivityEvent
from sqlalchemy.exc import IntegrityError

# A projector should already whitelist public display data.  This second
# boundary keeps an accidental future raw Runtime payload from becoming an SSE
# disclosure merely because a caller constructs UserActivityEvent directly.
_PRIVATE_PAYLOAD_KEYS = {
    "authorization",
    "capability",
    "checkpoint",
    "dedupe_key",
    "exception",
    "execution_id",
    "http_path",
    "http_status",
    "jwt",
    "lease",
    "mcp",
    "operation_id",
    "prompt",
    "raw_exception",
    "request_sent",
    "run_id",
    "sql",
    "stack",
    "step_id",
    "token",
    "tool_name",
    "trace_id",
    "traceback",
    "url",
}


activity_metadata = sa.MetaData()
user_activity_events = sa.Table(
    "user_activity_event",
    activity_metadata,
    # SQLite and PostgreSQL both assign this monotonically on insert.  It is
    # the only ordering/replay cursor exposed to clients.
    sa.Column("sequence", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("activity_id", sa.String(128), nullable=False, unique=True),
    sa.Column("conversation_id", sa.String(128), nullable=False),
    sa.Column("user_id", sa.String(128), nullable=False),
    sa.Column("tenant_id", sa.String(128), nullable=False),
    sa.Column("run_id", sa.String(128)),
    sa.Column("task_id", sa.String(128)),
    sa.Column("objective_id", sa.String(128)),
    sa.Column("resource_ref", sa.JSON),
    sa.Column("activity_type", sa.String(96), nullable=False),
    sa.Column("status", sa.String(64), nullable=False),
    sa.Column("display_key", sa.String(192), nullable=False),
    sa.Column("safe_payload", sa.JSON, nullable=False, default=dict),
    sa.Column("created_at", sa.String(64), nullable=False),
    sa.Column("verified_at", sa.String(64)),
    sa.Column("terminal", sa.Boolean, nullable=False, default=False),
    # A producer-controlled stable source key prevents duplicate delivery
    # from creating a second user action. It is never returned to the client.
    sa.Column("dedupe_key", sa.String(512)),
    sa.UniqueConstraint(
        "conversation_id",
        "dedupe_key",
        name="uq_user_activity_event_conversation_dedupe",
    ),
    sa.Index(
        "ix_user_activity_event_replay",
        "conversation_id",
        "user_id",
        "tenant_id",
        "sequence",
    ),
)


class UserActivityStoreProtocol(Protocol):
    def append(
        self,
        event: UserActivityEvent,
        *,
        user_id: str,
        tenant_id: str,
        dedupe_key: str = "",
    ) -> UserActivityEvent: ...

    def list_since(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[UserActivityEvent]: ...


class UserActivityStore:
    """In-process compatibility store used by explicit memory profiles only."""

    def __init__(self) -> None:
        self._events: list[tuple[UserActivityEvent, str, str, str]] = []
        self._by_dedupe: dict[tuple[str, str], UserActivityEvent] = {}
        self._sequence = 0
        self._lock = RLock()

    def append(
        self,
        event: UserActivityEvent,
        *,
        user_id: str,
        tenant_id: str,
        dedupe_key: str = "",
    ) -> UserActivityEvent:
        event = _public_event(event)
        with self._lock:
            key = (event.conversation_id, dedupe_key)
            if dedupe_key and key in self._by_dedupe:
                return self._by_dedupe[key].model_copy(deep=True)
            self._sequence += 1
            stored = event.model_copy(update={"sequence": self._sequence}, deep=True)
            self._events.append((stored, str(user_id), str(tenant_id), dedupe_key))
            if dedupe_key:
                self._by_dedupe[key] = stored
            return stored.model_copy(deep=True)

    def list_since(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[UserActivityEvent]:
        with self._lock:
            return [
                event.model_copy(deep=True)
                for event, owner_user, owner_tenant, _ in self._events
                if event.conversation_id == conversation_id
                and owner_user == str(user_id)
                and owner_tenant == str(tenant_id)
                and event.sequence > max(0, int(after_sequence))
            ][: max(1, min(int(limit), 500))]


MemoryUserActivityStore = UserActivityStore


class PostgresUserActivityStore:
    """SQLAlchemy store shared by API and standalone Worker processes."""

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            activity_metadata.create_all(bind)

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def append(
        self,
        event: UserActivityEvent,
        *,
        user_id: str,
        tenant_id: str,
        dedupe_key: str = "",
    ) -> UserActivityEvent:
        event = _public_event(event)
        normalized_key = str(dedupe_key or "")
        with self._connect() as conn:
            if normalized_key:
                existing = _select_by_dedupe(
                    conn,
                    conversation_id=event.conversation_id,
                    dedupe_key=normalized_key,
                )
                if existing is not None:
                    return _event_from_row(existing)
            values = _event_values(
                event,
                user_id=user_id,
                tenant_id=tenant_id,
                dedupe_key=normalized_key or None,
            )
            try:
                # Keep a uniqueness race isolated in a SAVEPOINT. PostgreSQL
                # otherwise marks the surrounding transaction aborted after
                # IntegrityError and prevents the authoritative reread below.
                with conn.begin_nested():
                    inserted = conn.execute(sa.insert(user_activity_events).values(**values))
            except IntegrityError:
                # A second Worker may have committed the same idempotent
                # result between the read and write. Return the first fact.
                if normalized_key:
                    existing = _select_by_dedupe(
                        conn,
                        conversation_id=event.conversation_id,
                        dedupe_key=normalized_key,
                    )
                    if existing is not None:
                        return _event_from_row(existing)
                raise
            sequence = inserted.inserted_primary_key[0]
            if sequence is None:
                row = conn.execute(
                    sa.select(user_activity_events)
                    .where(user_activity_events.c.activity_id == event.activity_id)
                ).mappings().one()
                return _event_from_row(row)
            return event.model_copy(update={"sequence": int(sequence)}, deep=True)

    def list_since(
        self,
        conversation_id: str,
        *,
        user_id: str,
        tenant_id: str,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> list[UserActivityEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(user_activity_events)
                .where(
                    user_activity_events.c.conversation_id == conversation_id,
                    user_activity_events.c.user_id == str(user_id),
                    user_activity_events.c.tenant_id == str(tenant_id),
                    user_activity_events.c.sequence > max(0, int(after_sequence)),
                )
                .order_by(user_activity_events.c.sequence.asc())
                .limit(max(1, min(int(limit), 500)))
            ).mappings().all()
        return [_event_from_row(row) for row in rows]


class _ConnectionContext:
    """Use a caller-owned SQLAlchemy Connection without closing it."""

    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sa.engine.Connection:
        return self._connection

    def __exit__(self, *_args: Any) -> None:
        return None


def _select_by_dedupe(
    conn: Any,
    *,
    conversation_id: str,
    dedupe_key: str,
) -> Any | None:
    return conn.execute(
        sa.select(user_activity_events)
        .where(
            user_activity_events.c.conversation_id == conversation_id,
            user_activity_events.c.dedupe_key == dedupe_key,
        )
    ).mappings().first()


def _event_values(
    event: UserActivityEvent,
    *,
    user_id: str,
    tenant_id: str,
    dedupe_key: str | None,
) -> dict[str, Any]:
    return {
        "activity_id": event.activity_id,
        "conversation_id": event.conversation_id,
        "user_id": str(user_id),
        "tenant_id": str(tenant_id),
        "run_id": event.run_id,
        "task_id": event.task_id,
        "objective_id": event.objective_id,
        "resource_ref": (
            event.resource_ref.model_dump(mode="json")
            if event.resource_ref is not None
            else None
        ),
        "activity_type": event.activity_type.value,
        "status": event.status.value,
        "display_key": event.display_key,
        "safe_payload": dict(event.safe_payload),
        "created_at": event.created_at,
        "verified_at": event.verified_at,
        "terminal": bool(event.terminal),
        "dedupe_key": dedupe_key,
    }


def _public_event(event: UserActivityEvent) -> UserActivityEvent:
    return event.model_copy(
        update={"safe_payload": _public_payload(event.safe_payload)},
        deep=True,
    )


def _public_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    sanitized: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).strip().lower()
        if normalized in _PRIVATE_PAYLOAD_KEYS or normalized.startswith(
            ("raw_", "debug_", "internal_")
        ) or any(
            fragment in normalized
            for fragment in ("password", "secret", "exception", "traceback")
        ):
            continue
        if isinstance(item, dict):
            sanitized[str(key)] = _public_payload(item)
        elif isinstance(item, list):
            sanitized[str(key)] = [
                _public_payload(entry) if isinstance(entry, dict) else entry
                for entry in item
                if not isinstance(entry, (bytes, bytearray))
            ]
        elif isinstance(item, (str, int, float, bool)) or item is None:
            sanitized[str(key)] = item
    return sanitized


def _event_from_row(row: Any) -> UserActivityEvent:
    values = dict(row)
    return UserActivityEvent.model_validate({
        "activity_id": values["activity_id"],
        "conversation_id": values["conversation_id"],
        "run_id": values.get("run_id"),
        "task_id": values.get("task_id"),
        "objective_id": values.get("objective_id"),
        "resource_ref": values.get("resource_ref"),
        "activity_type": values["activity_type"],
        "status": values["status"],
        "display_key": values["display_key"],
        "safe_payload": values.get("safe_payload") or {},
        "sequence": int(values["sequence"]),
        "created_at": values["created_at"],
        "verified_at": values.get("verified_at"),
        "terminal": bool(values.get("terminal")),
    })


__all__ = [
    "MemoryUserActivityStore",
    "PostgresUserActivityStore",
    "UserActivityStore",
    "UserActivityStoreProtocol",
    "activity_metadata",
    "user_activity_events",
]
