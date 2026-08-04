from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.exc import DBAPIError

from app.database import AgentEvent, append_event
from app.worker import _is_transient_exception, _public_execution_error


class _PostgresSessionStub:
    def __init__(self) -> None:
        self.executed: list[tuple[str, dict | None]] = []
        self.added: list[object] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def execute(self, statement, parameters=None):
        self.executed.append((str(statement), parameters))

    async def scalar(self, statement):
        return 7

    def add(self, value) -> None:
        self.added.append(value)


async def test_postgres_event_sequence_uses_advisory_not_run_row_lock() -> None:
    session = _PostgresSessionStub()

    await append_event(session, "run-1", "STEP_COMPLETED", {"step": 1})

    lock_sql, parameters = session.executed[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert "assistant_runs" not in lock_sql
    assert parameters == {"event_stream_key": "assistant-event:run-1"}
    assert len(session.added) == 1
    event = session.added[0]
    assert isinstance(event, AgentEvent)
    assert event.sequence == 8


def test_postgres_deadlock_and_serialization_errors_are_transient() -> None:
    class OriginalDatabaseError(Exception):
        def __init__(self, sqlstate: str) -> None:
            self.sqlstate = sqlstate

    deadlock = DBAPIError(None, None, OriginalDatabaseError("40P01"))
    serialization = DBAPIError(None, None, OriginalDatabaseError("40001"))
    invalid_input = DBAPIError(None, None, OriginalDatabaseError("22023"))

    assert _is_transient_exception(deadlock)
    assert _is_transient_exception(serialization)
    assert not _is_transient_exception(invalid_input)
    assert "自动重试" in _public_execution_error(deadlock, retrying=True)
    assert "点击重试" in _public_execution_error(deadlock, retrying=False)
    assert "40P01" not in _public_execution_error(deadlock, retrying=False)
