"""Immediate-accept Agent Run persistence and in-process Agent runner.

Design target 0813: the POST request must not wait for the first-turn LLM
reasoning. The request validates, persists the user message and a durable Run
(status ACCEPTED), and returns 202 immediately. An in-process Agent runner
claims ACCEPTED Runs (atomic DB claim + lease), executes the canonical
``adapter.execute`` path in the background, and pushes real business activity
events that the frontend maps to "正在查找相关内容…" etc.

Durability: the Run row is committed before 202; a crashed runner is
recovered by lease expiry (RUNNING -> reclaimable). Side-effecting actions
still go through the existing Durable Runtime unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from greenbook_contracts.events import (
    EVENT_FOLLOW_UP_QUEUED,
    EVENT_OBSERVATION,
    EVENT_REASONING_STARTED,
    EVENT_RUN_COMPLETED,
    EVENT_RUN_FAILED,
    EVENT_SEMANTIC_ACTION,
    EVENT_TOOL_STARTED,
    EVENT_WAITING_APPROVAL,
)
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

RUN_ACCEPTED = "ACCEPTED"
RUN_RUNNING = "RUNNING"
RUN_COMPLETED = "COMPLETED"
RUN_PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
RUN_FAILED = "FAILED"
RUN_CANCELLED = "CANCELLED"
RUN_WAITING = "WAITING_USER"
RUN_WAITING_APPROVAL = "WAITING_APPROVAL"

RUN_TERMINAL = {
    RUN_COMPLETED,
    RUN_PARTIAL_SUCCESS,
    RUN_FAILED,
    RUN_CANCELLED,
}

# Statuses where a Run is still actively working. A follow-up Run linked via
# ``payload.follow_up_of`` waits (stays unclaimed) while its parent is in one
# of these states, so a mid-turn instruction is processed after the current
# work instead of racing it as an independent parallel card (design 0813).
RUN_WORKING = frozenset({
    RUN_ACCEPTED,
    RUN_RUNNING,
    "RETRYING",
    "WAITING_DEPENDENCY",
    "WAITING_LANE",
    "WAITING_APPROVAL",
    "PAUSED",
})


def performance_projection(run: "AgentRun", result: Any) -> dict[str, Any]:
    """Persist only per-Run measurements that have a concrete source."""

    partial = getattr(result, "partial_results", None)
    partial = partial if isinstance(partial, dict) else {}
    now = datetime.now(UTC)
    try:
        started = datetime.fromisoformat(str(run.created_at).replace("Z", "+00:00"))
        total_latency_ms: int | None = max(0, round((now - started).total_seconds() * 1000))
    except (TypeError, ValueError):
        total_latency_ms = None
    tool_calls = partial.get("tool_calls", getattr(result, "tool_rounds", None))
    try:
        from greenbook_agent_core.observability.run_metrics import snapshot
        boundary = snapshot(run.run_id)
    except Exception:
        boundary = {}
    metrics = {
        "total_latency_ms": total_latency_ms,
        "first_response_latency_ms": None,
        "llm_calls": partial.get("llm_calls", boundary.get("llm_calls")),
        "llm_latency_ms": boundary.get("llm_latency_ms"),
        "semantic_llm_calls": boundary.get("semantic_llm_calls"),
        "semantic_llm_latency_ms": boundary.get("semantic_llm_latency_ms"),
        "semantic_input_tokens": boundary.get("semantic_input_tokens"),
        "semantic_output_tokens": boundary.get("semantic_output_tokens"),
        "creator_llm_calls": boundary.get("creator_llm_calls", 0),
        "creator_latency_ms": boundary.get("creator_latency_ms", 0),
        "creator_input_tokens": boundary.get("creator_input_tokens", 0),
        "creator_output_tokens": boundary.get("creator_output_tokens", 0),
        "actionloop_iterations": partial.get("iterations", boundary.get("actionloop_iterations")),
        "actionloop_llm_calls": boundary.get("actionloop_llm_calls"),
        "actionloop_llm_latency_ms": boundary.get("actionloop_llm_latency_ms"),
        "actionloop_input_tokens": boundary.get("actionloop_input_tokens"),
        "actionloop_output_tokens": boundary.get("actionloop_output_tokens"),
        "tool_calls": int(tool_calls) if isinstance(tool_calls, int) else None,
        "tool_latency_ms": boundary.get("tool_latency_ms"),
        "java_calls": partial.get("java_calls", boundary.get("java_calls")),
        "java_latency_ms": boundary.get("java_latency_ms"),
        "input_tokens": partial.get("input_tokens", boundary.get("input_tokens")),
        "output_tokens": partial.get("output_tokens", boundary.get("output_tokens")),
        "final_response_latency_ms": boundary.get("final_response_latency_ms"),
        "stage_timestamps": boundary.get("stage_timestamps", {}),
        "stage_durations_ms": boundary.get("stage_durations_ms", {}),
        "memory_retrieval": boundary.get("memory_retrieval", {}),
        "llm_events": boundary.get("llm_events", []),
    }
    durations = metrics["stage_durations_ms"] or {}
    skipped = bool((boundary.get("stage_timestamps") or {}).get("memory_recall_skipped"))
    metrics.update({
        "memory_total_ms": 0 if skipped and durations.get("memory_retrieval_ms") is None else durations.get("memory_retrieval_ms"),
        "memory_search_ms": 0 if skipped and durations.get("memory_repository_search_ms") is None else durations.get("memory_repository_search_ms"),
        "memory_rank_ms": 0 if skipped and durations.get("memory_ranking_filter_ms") is None else durations.get("memory_ranking_filter_ms"),
        "memory_touch_ms": 0 if skipped and durations.get("memory_touch_ms") is None else durations.get("memory_touch_ms"),
        "memory_format_ms": 0 if skipped and durations.get("memory_format_ms") is None else durations.get("memory_format_ms"),
    })
    if boundary.get("tool_calls") is not None:
        metrics["tool_calls"] = max(metrics["tool_calls"] or 0, boundary["tool_calls"])
    return metrics


class AgentRun(BaseModel):
    """Durable immediate-accept Run record."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    conversation_id: str
    user_id: str
    tenant_id: str
    idempotency_key: str = ""
    status: str = RUN_ACCEPTED
    claimed_by: str = ""
    lease_until: str = ""
    version: int = 0
    payload: dict[str, Any] = {}
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""


class AgentRunEvent(BaseModel):
    """One business activity event for a Run."""

    model_config = ConfigDict(extra="forbid")

    event_id: int = 0
    run_id: str
    event_type: str
    payload: dict[str, Any] = {}
    created_at: str = ""


class AgentRunStore:
    """PostgreSQL-backed Run state with atomic claim and lease recovery."""

    TABLE_NAME = "agent_runs"

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        self._table = self._build_table()
        if create_tables:
            self._table.create(self._bind, checkfirst=True)
            if getattr(bind, "dialect", None) is not None and bind.dialect.name == "postgresql":
                with bind.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS "
                        "claimed_by VARCHAR(128) NOT NULL DEFAULT ''"
                    )
                    connection.exec_driver_sql(
                        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS "
                        "lease_until VARCHAR(64) NOT NULL DEFAULT ''"
                    )
                    connection.exec_driver_sql(
                        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS "
                        "version INTEGER NOT NULL DEFAULT 0"
                    )
                    connection.exec_driver_sql(
                        "ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS "
                        "idempotency_key VARCHAR(256)"
                    )
            elif getattr(bind, "dialect", None) is not None and bind.dialect.name == "sqlite":
                # SQLite has no ADD COLUMN IF NOT EXISTS.  Test/dev databases
                # may already have the table from an earlier schema revision.
                with bind.connect() as connection:
                    columns = {
                        str(column[1])
                        for column in connection.exec_driver_sql(
                            f"PRAGMA table_info({self.TABLE_NAME})"
                        ).fetchall()
                    }
                if "idempotency_key" not in columns:
                    with bind.begin() as connection:
                        connection.exec_driver_sql(
                            "ALTER TABLE agent_runs ADD COLUMN idempotency_key VARCHAR(256)"
                        )
            with bind.begin() as connection:
                connection.exec_driver_sql(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ux_agent_runs_idempotency_scope "
                    "ON agent_runs (conversation_id, user_id, tenant_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL AND idempotency_key <> ''"
                )

    @classmethod
    def _build_table(cls) -> sa.Table:
        return sa.Table(
            cls.TABLE_NAME,
            sa.MetaData(),
            sa.Column("run_id", sa.Text, primary_key=True),
            sa.Column("conversation_id", sa.Text, nullable=False, default=""),
            sa.Column("user_id", sa.Text, nullable=False, default=""),
            sa.Column("tenant_id", sa.Text, nullable=False, default=""),
            sa.Column("idempotency_key", sa.Text, nullable=True),
            sa.Column("status", sa.Text, nullable=False, default=RUN_ACCEPTED),
            sa.Column("claimed_by", sa.Text, nullable=False, default=""),
            sa.Column("lease_until", sa.Text, nullable=False, default=""),
            sa.Column("version", sa.Integer, nullable=False, default=0),
            sa.Column("payload", sa.JSON, nullable=False, default=dict),
            sa.Column("error_code", sa.Text, nullable=False, default=""),
            sa.Column("error_message", sa.Text, nullable=False, default=""),
            sa.Column("created_at", sa.Text, nullable=False),
            sa.Column("updated_at", sa.Text, nullable=False),
        )

    def create(self, run: AgentRun) -> AgentRun:
        key = str(
            run.idempotency_key
            or (run.payload or {}).get("idempotency_key")
            or ""
        ).strip()
        try:
            with self._bind.begin() as connection:
                connection.execute(
                    self._table.insert().values(
                        run_id=run.run_id,
                        conversation_id=run.conversation_id,
                        user_id=run.user_id,
                        tenant_id=run.tenant_id,
                        idempotency_key=key or None,
                        status=run.status,
                        claimed_by=run.claimed_by,
                        lease_until=run.lease_until,
                        version=run.version,
                        payload=dict(run.payload),
                        error_code=run.error_code,
                        error_message=run.error_message,
                        created_at=run.created_at or _now(),
                        updated_at=run.updated_at or _now(),
                    )
                )
            return run.model_copy(update={"idempotency_key": key})
        except IntegrityError:
            existing = self.get_by_idempotency_key(
                conversation_id=run.conversation_id,
                user_id=run.user_id,
                tenant_id=run.tenant_id,
                idempotency_key=key,
            )
            if existing is not None:
                return existing
            raise

    def get_by_idempotency_key(
        self,
        *,
        conversation_id: str,
        user_id: str,
        tenant_id: str,
        idempotency_key: str,
    ) -> AgentRun | None:
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._bind.connect() as connection:
            row = connection.execute(
                sa.select(self._table)
                .where(
                    self._table.c.conversation_id == conversation_id,
                    self._table.c.user_id == user_id,
                    self._table.c.tenant_id == tenant_id,
                    self._table.c.idempotency_key == key,
                )
                .order_by(self._table.c.created_at)
                .limit(1)
            ).mappings().first()
        return _row_to_run(row) if row else None

    def get(self, run_id: str) -> AgentRun | None:
        with self._bind.connect() as connection:
            row = connection.execute(
                sa.select(self._table).where(self._table.c.run_id == run_id)
            ).mappings().first()
        return _row_to_run(row) if row else None

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int = 300,
        limit: int = 1,
        max_concurrent_per_conversation: int | None = None,
    ) -> list[AgentRun]:
        """Atomically claim ACCEPTED Runs with optional conversation fairness.

        There is intentionally no conversation-wide mutex here.  A bounded
        per-conversation count only prevents one conversation from consuming
        every runner slot; independent conversations and independent Runs in
        the same conversation can still be claimed together.
        """
        now = _now()
        with self._bind.begin() as connection:
            if getattr(self._bind.dialect, "name", "") == "postgresql":
                # Serialize only the short claim transaction so independent
                # runner processes see one consistent per-conversation count.
                # Execution/reasoning never holds this advisory lock.
                connection.exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(hashtext('greenbook.agent-run-claim'))"
                )
            connection.execute(
                sa.update(self._table)
                .where(
                    self._table.c.status == RUN_RUNNING,
                    self._table.c.lease_until != "",
                    self._table.c.lease_until <= now,
                )
                .values(status=RUN_ACCEPTED, claimed_by="", lease_until="")
            )
            candidate_limit = max(1, limit)
            candidates = connection.execute(
                sa.select(self._table)
                .where(self._table.c.status == RUN_ACCEPTED)
                .order_by(self._table.c.created_at)
                .limit(candidate_limit * 4 if max_concurrent_per_conversation else candidate_limit)
            ).mappings().all()
            running_by_conversation: dict[str, int] = {}
            if max_concurrent_per_conversation is not None:
                counts = connection.execute(
                    sa.select(
                        self._table.c.conversation_id,
                        sa.func.count().label("count"),
                    )
                    .where(self._table.c.status == RUN_RUNNING)
                    .group_by(self._table.c.conversation_id)
                ).all()
                running_by_conversation = {
                    str(row[0]): int(row[1] or 0) for row in counts
                }
            claimed: list[Any] = []
            # Resolve follow-up parent statuses once so candidates whose
            # parent is still working are left unclaimed (they wait until the
            # parent reaches a terminal state).
            follow_up_parent_ids = {
                str((dict(candidate["payload"] or {})).get("follow_up_of") or "")
                for candidate in candidates
            } - {""}
            parent_statuses: dict[str, str] = {}
            if follow_up_parent_ids:
                parent_rows = connection.execute(
                    sa.select(
                        self._table.c.run_id,
                        self._table.c.status,
                    ).where(self._table.c.run_id.in_(follow_up_parent_ids))
                ).all()
                parent_statuses = {
                    str(row[0]): str(row[1] or "") for row in parent_rows
                }
            for candidate in candidates:
                if len(claimed) >= candidate_limit:
                    break
                conversation_id = str(candidate["conversation_id"] or "")
                if (
                    max_concurrent_per_conversation is not None
                    and running_by_conversation.get(conversation_id, 0)
                    >= max(1, max_concurrent_per_conversation)
                ):
                    continue
                follow_up_of = str(
                    (dict(candidate["payload"] or {})).get("follow_up_of") or ""
                )
                if (
                    follow_up_of
                    and parent_statuses.get(follow_up_of, "") in RUN_WORKING
                ):
                    # The parent is still working; keep this Run ACCEPTED and
                    # let a later claim poll pick it up once the parent ends.
                    continue
                row = connection.execute(
                    self._table.update()
                    .where(
                        self._table.c.run_id == candidate["run_id"],
                        self._table.c.status == RUN_ACCEPTED,
                    )
                    .values(
                        status=RUN_RUNNING,
                        claimed_by=worker_id,
                        lease_until=_lease_until(now, lease_seconds),
                        version=self._table.c.version + 1,
                        updated_at=now,
                    )
                    .returning(self._table)
                ).mappings().first()
                if row is None:
                    continue
                claimed.append(row)
                running_by_conversation[conversation_id] = (
                    running_by_conversation.get(conversation_id, 0) + 1
                )
        return [_row_to_run(row) for row in claimed]

    def mark_status(
        self,
        run_id: str,
        status: str,
        *,
        error_code: str = "",
        error_message: str = "",
        claimed_by: str | None = None,
        expected_version: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        with self._bind.begin() as connection:
            conditions = [self._table.c.run_id == run_id]
            # A Run is a user-facing projection with a terminal latch.  A
            # late callback may repeat the same terminal status, but it must
            # never reopen or replace a terminal outcome with a stale
            # non-terminal projection.
            if status in RUN_TERMINAL:
                conditions.append(
                    sa.or_(
                        self._table.c.status.notin_(RUN_TERMINAL),
                        self._table.c.status == status,
                    )
                )
            else:
                conditions.append(self._table.c.status.notin_(RUN_TERMINAL))
            if claimed_by is not None:
                conditions.append(self._table.c.claimed_by == claimed_by)
            if expected_version is not None:
                conditions.append(self._table.c.version == expected_version)
            values: dict[str, Any] = {
                "status": status,
                "error_code": error_code,
                "error_message": error_message,
                "claimed_by": "",
                "lease_until": "",
                "version": self._table.c.version + 1,
                "updated_at": _now(),
            }
            if payload is not None:
                values["payload"] = dict(payload)
            result = connection.execute(
                self._table.update()
                .where(*conditions)
                .values(**values)
            )
        return bool(result.rowcount)

    def list_active(self) -> list[AgentRun]:
        with self._bind.connect() as connection:
            rows = connection.execute(
                sa.select(self._table)
                .where(self._table.c.status.in_([RUN_ACCEPTED, RUN_RUNNING]))
                .order_by(self._table.c.created_at)
            ).mappings().all()
        return [_row_to_run(row) for row in rows]

    def list_recent(self, *, limit: int = 30) -> list[AgentRun]:
        """Return recent durable Runs for refresh/recovery projections."""

        with self._bind.connect() as connection:
            rows = connection.execute(
                sa.select(self._table)
                .order_by(self._table.c.created_at.desc())
                .limit(max(1, limit))
            ).mappings().all()
        return [_row_to_run(row) for row in rows]


class AgentRunEventStore:
    """Run-level business activity events (same-process SSE + refresh fallback)."""

    def __init__(self) -> None:
        self._events: dict[str, list[AgentRunEvent]] = {}

    def append(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> AgentRunEvent:
        events = self._events.setdefault(run_id, [])
        event = AgentRunEvent(
            event_id=len(events) + 1,
            run_id=run_id,
            event_type=event_type,
            payload=dict(payload or {}),
            created_at=_now(),
        )
        events.append(event)
        return event

    def list_since(self, run_id: str, after_event_id: int = 0) -> list[AgentRunEvent]:
        events = self._events.get(run_id, [])
        return [event for event in events if event.event_id > after_event_id]

    def clear(self, run_id: str) -> None:
        self._events.pop(run_id, None)


def project_progressive_event(
    semantic_action: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Project a real Observation into a safe user-facing partial result.

    Pure projection: never decides the next action and never judges Goal
    completion. Only business facts already present in the result become a
    partial result ("找到 N 篇"), so no fake progress can be emitted.
    """

    action = str(semantic_action or "").upper().replace("-", "_")
    data = result.get("data") or result.get("payload") or {}
    if not isinstance(data, dict):
        data = {}
    records: list[Any] = []
    for key in ("items", "posts", "results"):
        value = data.get(key)
        if isinstance(value, list):
            records = value
            break
    count = None
    for key in ("total", "count", "total_count"):
        raw = data.get(key)
        if isinstance(raw, (int, float)):
            count = int(raw)
            break
    if count is None and records:
        count = len(records)

    if action in {"SEARCH_COMMUNITY", "LIST_OWN_POSTS"}:
        if count is None and not records:
            return None
        return {
            "activity_type": "SEARCH_SUMMARY",
            "title": "找到相关内容",
            "count": count,
            "status": "SUCCESS",
        }
    if action in {"GET_POST_DETAIL", "READ_CONTENT", "READ_POST", "GET_POST"}:
        if not records:
            return None
        return {
            "activity_type": "EVIDENCE_SELECTION",
            "title": "已阅读重点内容",
            "count": len(records),
            "status": "SUCCESS",
        }
    if action == "GENERATE_CONTENT":
        draft_id = str(result.get("draft_id") or data.get("draft_id") or data.get("resource_id") or "")
        if not draft_id:
            return None
        return {
            "activity_type": "DRAFT_CREATED",
            "title": "草稿已生成",
            "business_result": {"draft_id": draft_id},
            "status": "SUCCESS",
        }
    if action == "SCHEDULE_PUBLISH":
        schedule_id = str(result.get("schedule_id") or data.get("schedule_id") or data.get("resource_id") or "")
        if not schedule_id:
            return None
        run_at = str(
            result.get("run_at")
            or data.get("run_at")
            or data.get("runAt")
            or ""
        )
        return {
            "activity_type": "SCHEDULED",
            "title": "已安排发布时间",
            "run_at": run_at,
            "business_result": {"schedule_id": schedule_id, "run_at": run_at},
            "status": "SUCCESS",
        }
    return None


AgentRunResultHandler = Callable[..., Awaitable[None]]


class AgentRunner:
    """Claim durable Runs and execute them through the canonical adapter.

    ``result_handler`` receives the finished RuntimeResult (persisting
    projections, run_store, assistant message) and is supplied by the API
    wiring so the runner stays transport-agnostic.
    """

    def __init__(
        self,
        *,
        run_store: AgentRunStore,
        event_store: AgentRunEventStore,
        execute: Callable[..., Awaitable[Any]],
        result_handler: AgentRunResultHandler,
        worker_id: str = "agent-runner",
        poll_interval_seconds: float = 0.5,
        lease_seconds: int = 300,
        shutdown_event: Any = None,
        max_concurrent_runs: int = 4,
        max_concurrent_per_conversation: int = 2,
    ) -> None:
        self._run_store = run_store
        self._event_store = event_store
        self._execute = execute
        self._result_handler = result_handler
        self._worker_id = worker_id
        self._poll_interval = poll_interval_seconds
        self._lease_seconds = lease_seconds
        self._shutdown = shutdown_event or asyncio.Event()
        self._max_concurrent_runs = max(1, max_concurrent_runs)
        self._max_concurrent_per_conversation = max(
            1, max_concurrent_per_conversation
        )
        self._processing: dict[str, asyncio.Task[None]] = {}

    def _recover_non_submitted_runs(self) -> None:
        """Close invalid pre-write RUNNING latches without retrying them.

        A pre-write ActionLoop exception used to be projected as
        ``WAITING_EXTERNAL`` and left a Run RUNNING with no execution or lease.
        Such a row cannot be safely retried and would block its queued
        follow-ups forever.  Only the narrow, observable zero-write shape is
        closed here; submitted/unknown work remains untouched.
        """

        for run in self._run_store.list_active():
            if run.status != RUN_RUNNING:
                continue
            # A process that died after claiming/projection must leave either
            # a lease or a terminal status.  A RUNNING row with neither is an
            # invalid latch and otherwise blocks every follow-up in the same
            # conversation forever.
            payload = dict(run.payload or {})
            partial_results = dict(payload.get("partial_results") or {})
            has_execution = bool(
                payload.get("execution_id")
                or payload.get("execution_ids")
                or partial_results.get("execution_ids")
            )
            if (
                not str(run.claimed_by or "")
                and not str(run.lease_until or "")
                and not has_execution
            ):
                self._run_store.mark_status(
                    run.run_id,
                    RUN_FAILED,
                    error_code="AGENT_RUN_STALE_LATCH",
                    error_message="任务运行状态已失效，请重新提交。",
                    expected_version=run.version,
                )
                continue
            if run.error_code != "ACTION_LOOP_AFTER_SIDE_EFFECT":
                continue
            performance = dict(payload.get("performance") or {})
            if (
                payload.get("execution_id")
                or payload.get("execution_ids")
                or partial_results.get("execution_ids")
            ):
                continue
            if any(
                int(performance.get(key) or 0) != 0
                for key in ("actionloop_iterations", "tool_calls", "java_calls")
            ):
                continue
            self._run_store.mark_status(
                run.run_id,
                RUN_FAILED,
                error_code="ACTION_LOOP_NO_PROGRESS",
                error_message="执行在产生副作用前中断，未提交写入。",
                claimed_by=run.claimed_by,
                expected_version=run.version,
            )

    async def run(self) -> None:
        self._recover_non_submitted_runs()
        while not self._shutdown.is_set():
            try:
                capacity = self._max_concurrent_runs - len(self._processing)
                claimed = (
                    self._run_store.claim(
                        worker_id=self._worker_id,
                        lease_seconds=self._lease_seconds,
                        limit=capacity,
                        max_concurrent_per_conversation=(
                            self._max_concurrent_per_conversation
                        ),
                    )
                    if capacity > 0
                    else []
                )
                for run in claimed:
                    try:
                        from greenbook_agent_core.observability.run_metrics import record_stage
                        record_stage("worker_claimed", run_id=run.run_id)
                    except Exception:
                        pass
                    self._processing[run.run_id] = asyncio.create_task(
                        self._process(run),
                        name=f"agent-run:{run.run_id}",
                    )
                completed = [
                    (run_id, task)
                    for run_id, task in self._processing.items()
                    if task.done()
                ]
                for run_id, task in completed:
                    self._processing.pop(run_id, None)
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        task.result()
            except Exception:
                logger.warning("Agent runner poll failed", exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._shutdown.wait(), timeout=self._poll_interval)
        await self._stop_processing()

    def request_shutdown(self) -> None:
        self._shutdown.set()

    async def _stop_processing(self) -> None:
        tasks = list(self._processing.values())
        self._processing.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process(self, run: AgentRun) -> None:
        started_at = time.perf_counter()
        try:
            from greenbook_agent_core.observability.run_metrics import record_stage
            record_stage("worker_started", run_id=run.run_id)
            record_stage("runner_started", run_id=run.run_id)
            record_stage("queue_wait_end", run_id=run.run_id)
        except Exception:
            pass
        self._event_store.append(run.run_id, EVENT_REASONING_STARTED, {"run_id": run.run_id})
        try:
            result = await self._execute(run)
        except Exception as exc:
            logger.exception("Agent run execution failed run_id=%s", run.run_id)
            self._run_store.mark_status(
                run.run_id,
                RUN_FAILED,
                error_code="AGENT_RUN_FAILED",
                error_message=str(exc) or "Agent run failed",
                claimed_by=run.claimed_by,
                expected_version=run.version,
            )
            self._event_store.append(run.run_id, EVENT_RUN_FAILED, {
                "run_id": run.run_id,
                "error": str(exc) or "Agent run failed",
            })
            return
        try:
            await self._result_handler(run, result)
        except Exception as exc:
            logger.exception("Agent run result projection failed run_id=%s", run.run_id)
            # A projection failure must not leave a claimed Run in RUNNING
            # forever.  The business execution has already returned; this is
            # a user-visible terminal error, not permission to retry a write.
            self._run_store.mark_status(
                run.run_id,
                RUN_FAILED,
                error_code="RUN_RESULT_PROJECTION_FAILED",
                error_message="任务结果同步失败，请稍后重试。",
                claimed_by=run.claimed_by,
                expected_version=run.version,
            )
            self._event_store.append(run.run_id, EVENT_RUN_FAILED, {
                "run_id": run.run_id,
                "error": str(exc) or "Run result projection failed",
            })
        result_status = str(getattr(result, "status", "") or "").upper()
        waiting = result_status in {"WAITING_HUMAN", "WAITING_APPROVAL", "ASK_USER"}
        result_execution_id = str(getattr(result, "execution_id", "") or "")
        result_approval_id = str(getattr(result, "approval_id", "") or "")
        approval_waiting = bool(
            waiting and result_execution_id and result_approval_id
        )
        partial_results = getattr(result, "partial_results", {}) or {}
        parallel_results = (
            partial_results.get("parallel_results", [])
            if isinstance(partial_results, dict)
            else []
        )
        # WAITING_EXTERNAL is a successful durable submission boundary, not a
        # terminal Run result.  Keep the Run RUNNING even when an older adapter
        # has not copied execution_ids into partial_results yet.
        accepted_execution = result_status in {
            "RUNNING",
            "QUEUED",
            "SUBMITTED",
            "WAITING_EXTERNAL",
        }
        if not accepted_execution and isinstance(partial_results, dict):
            accepted_execution = bool(partial_results.get("execution_ids")) or any(
                str(item.get("status") or "").upper()
                in {"RUNNING", "QUEUED", "SUBMITTED", "WAITING_EXTERNAL"}
                for item in parallel_results
                if isinstance(item, dict)
            )
        payload_update = dict(run.payload or {})
        payload_changed = False
        performance = performance_projection(run, result)
        performance["runner_latency_ms"] = round((time.perf_counter() - started_at) * 1000)
        payload_update["performance"] = performance
        payload_changed = True
        if isinstance(partial_results, dict):
            for key, value in (
                ("execution_id", result_execution_id),
                ("approval_id", result_approval_id),
            ):
                if value and payload_update.get(key) != value:
                    payload_update[key] = value
                    payload_changed = True
            for key in ("execution_ids", "task_ids"):
                values = [str(item) for item in (partial_results.get(key) or []) if item]
                if values:
                    merged = list(dict.fromkeys(
                        [str(item) for item in (payload_update.get(key) or []) if item]
                        + values
                    ))
                    if merged != payload_update.get(key):
                        payload_update[key] = merged
                        payload_changed = True
            # Keep the first structured failure separate from the terminal
            # loop guard.  The durable Run is the recovery/audit authority;
            # callers must not lose the root cause when the AgentLoop stops on
            # NO_PROGRESS_DETECTED.
            for key in ("root_failure", "reasoning_failure"):
                value = partial_results.get(key)
                if value:
                    persisted_partial = dict(payload_update.get("partial_results") or {})
                    if persisted_partial.get(key) != value:
                        persisted_partial[key] = value
                        payload_update["partial_results"] = persisted_partial
                        payload_changed = True
            # Keep the canonical confirmation identity/version on the
            # existing durable Run.  This is the recovery marker for the
            # Task-CAS -> resume handoff; it is not a new confirmation store.
            confirmation = partial_results.get("semantic_confirmation")
            if isinstance(confirmation, dict):
                persisted_partial = dict(payload_update.get("partial_results") or {})
                if persisted_partial.get("semantic_confirmation") != confirmation:
                    persisted_partial["semantic_confirmation"] = dict(confirmation)
                    payload_update["partial_results"] = persisted_partial
                    payload_changed = True
        if waiting:
            payload_update["waiting_state"] = result_status
            payload_changed = True
        next_status = (
            RUN_RUNNING
            if accepted_execution
            else (
                RUN_COMPLETED
                if getattr(result, "success", False)
                else (
                    RUN_WAITING_APPROVAL
                    if approval_waiting
                    else (RUN_WAITING if waiting else RUN_FAILED)
                )
            )
        )
        self._run_store.mark_status(
            run.run_id,
            next_status,
            error_code=getattr(result, "error_code", "") or "",
            error_message=getattr(result, "error_message", "") or getattr(result, "error", "") or "",
            claimed_by=run.claimed_by,
            expected_version=run.version,
            payload=payload_update if payload_changed else None,
        )
        if accepted_execution:
            self._event_store.append(
                run.run_id,
                "EXECUTION_ACCEPTED",
                {
                    "run_id": run.run_id,
                    "execution_pending": True,
                },
            )
        elif getattr(result, "success", False):
            self._event_store.append(
                run.run_id,
                EVENT_RUN_COMPLETED,
                {"run_id": run.run_id},
            )
        elif waiting:
            self._event_store.append(run.run_id, EVENT_WAITING_APPROVAL, {
                "run_id": run.run_id,
                "error": getattr(result, "error_message", "") or getattr(result, "error", "") or "",
            })
        else:
            self._event_store.append(run.run_id, EVENT_RUN_FAILED, {
                "run_id": run.run_id,
                "error": getattr(result, "error_message", "") or getattr(result, "error", "") or "",
            })


def _row_to_run(row: Any) -> AgentRun:
    return AgentRun(
        run_id=str(row["run_id"]),
        conversation_id=str(row["conversation_id"] or ""),
        user_id=str(row["user_id"] or ""),
        tenant_id=str(row["tenant_id"] or ""),
        idempotency_key=str(row.get("idempotency_key") or "") if hasattr(row, "get") else "",
        status=str(row["status"] or RUN_ACCEPTED),
        claimed_by=str(row["claimed_by"] or ""),
        lease_until=str(row["lease_until"] or ""),
        version=int(row["version"] or 0),
        payload=dict(row["payload"] or {}),
        error_code=str(row["error_code"] or ""),
        error_message=str(row["error_message"] or ""),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _lease_until(now: str, lease_seconds: int) -> str:
    parsed = datetime.fromisoformat(now)
    return datetime.fromtimestamp(
        parsed.timestamp() + max(1, lease_seconds),
        tz=UTC,
    ).isoformat()


__all__ = [
    "AgentRun",
    "AgentRunEvent",
    "AgentRunEventStore",
    "AgentRunStore",
    "AgentRunner",
    "EVENT_OBSERVATION",
    "EVENT_REASONING_STARTED",
    "EVENT_RUN_COMPLETED",
    "EVENT_RUN_FAILED",
    "EVENT_SEMANTIC_ACTION",
    "EVENT_TOOL_STARTED",
    "EVENT_WAITING_APPROVAL",
    "EVENT_FOLLOW_UP_QUEUED",
    "RUN_ACCEPTED",
    "RUN_CANCELLED",
    "RUN_COMPLETED",
    "RUN_PARTIAL_SUCCESS",
    "RUN_FAILED",
    "RUN_RUNNING",
    "RUN_TERMINAL",
    "RUN_WAITING",
    "RUN_WORKING",
]
