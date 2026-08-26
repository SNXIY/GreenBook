"""ActionObservation — durable business evidence for AgentLoop continuation.

One thin record per terminal incremental Execution. Written after the durable
completion projection commits, consumed idempotently by the continuation
consumer, and used to resume AgentLoop with real business results (draft_id,
schedule_id, artifacts) instead of re-deriving intent from the user message.

The observation is self-contained: ``payload`` carries the GoalTree snapshot,
the original Command, and the session so a later process can rebuild the
AgentLoop context without re-interpreting the user request.
"""

from __future__ import annotations

import inspect
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from greenbook_contracts.identity import AuthContext
from pydantic import BaseModel, ConfigDict, Field

from ..artifact.store import ArtifactStore
from .execution_queue import ExecutionQueueMessage
from .result_resolver import ResultResolver
from .runtime_result import RuntimeResult

INCREMENTAL_PLAN_SOURCE = "AGENT_INCREMENTAL"
OBSERVATION_PENDING = "PENDING"
OBSERVATION_DISPATCHED = "DISPATCHED"
OBSERVATION_DONE = "DONE"

logger = logging.getLogger(__name__)


class ActionObservation(BaseModel):
    """Structured business result of one terminal durable action."""

    model_config = ConfigDict(extra="forbid")

    observation_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    execution_id: str = Field(min_length=1)
    task_id: str = ""
    conversation_id: str = ""
    # Run-level ownership so a continuation can write activity events back to
    # the same Run stream (progressive UX across AgentLoop rounds).
    run_id: str = ""
    goal_id: str = ""
    capability: str = ""
    status: str = ""  # COMPLETED | FAILED | CANCELLED
    draft_id: str = ""
    schedule_id: str = ""
    artifact_refs: list[str] = Field(default_factory=list)
    resource_refs: list[dict[str, Any]] = Field(default_factory=list)
    business_result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    observed_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    state: str = OBSERVATION_PENDING
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status.upper() in {"COMPLETED", "FAILED", "CANCELLED"}


class ActionObservationStore:
    """In-memory observation store used by tests and the memory profile."""

    def __init__(self) -> None:
        self._observations: dict[str, ActionObservation] = {}
        self._claimed: dict[str, float] = {}

    def save(self, observation: ActionObservation) -> ActionObservation:
        # Idempotent by execution_id: a repeated terminal hook must not create
        # a second continuation record.
        existing = self.get_by_execution(observation.execution_id)
        if existing is not None:
            return existing
        self._observations[observation.observation_id] = observation
        return observation

    def get_by_execution(self, execution_id: str) -> ActionObservation | None:
        return next(
            (
                item
                for item in self._observations.values()
                if item.execution_id == execution_id
            ),
            None,
        )

    def list_recent_for_tasks(
        self,
        task_ids: list[str] | tuple[str, ...] | set[str],
        *,
        limit: int = 8,
    ) -> list[ActionObservation]:
        """Return a bounded receipt projection for the supplied Tasks."""

        wanted = {str(item) for item in task_ids if str(item or "")}
        if not wanted or limit <= 0:
            return []
        values = [
            item
            for item in self._observations.values()
            if str(item.task_id or "") in wanted
        ]
        return sorted(values, key=lambda item: item.observed_at, reverse=True)[:limit]

    def list_pending(self) -> list[ActionObservation]:
        return [
            item
            for item in sorted(self._observations.values(), key=lambda item: item.observed_at)
            if item.state == OBSERVATION_PENDING
        ]

    def claim_pending(
        self,
        batch_size: int = 1,
        dispatch_timeout_seconds: int = 600,
    ) -> list[ActionObservation]:
        # Recover DISPATCHED observations whose consumer crashed before
        # marking them DONE.  The durable predecessor claim below is the
        # concurrent caller guard; recovery preserves the existing retry
        # boundary without widening the claim to semantic_action.
        if dispatch_timeout_seconds >= 0:
            self._recover_dispatched(dispatch_timeout_seconds)
        claimed: list[ActionObservation] = []
        for item in self.list_pending():
            if len(claimed) >= batch_size:
                break
            if self._claim_item(item):
                claimed.append(item)
        return claimed

    def claim_continuation(
        self,
        execution_id: str,
        dispatch_timeout_seconds: int = 600,
    ) -> ActionObservation | None:
        """Atomically claim the continuation for one verified predecessor.

        ``execution_id`` is the durable identity of the verified progression
        that opens the next ActionLoop turn.  It is intentionally narrower
        than ``(task_id, semantic_action)``: a later legitimate repeat has a
        different predecessor execution and therefore a different claim.
        """

        if not str(execution_id or "").strip():
            return None
        if dispatch_timeout_seconds >= 0:
            self._recover_dispatched(dispatch_timeout_seconds)
        item = self.get_by_execution(str(execution_id))
        if item is None or item.state != OBSERVATION_PENDING:
            return None
        if self._claim_item(item):
            return item
        return None

    def _claim_item(self, item: ActionObservation) -> bool:
        if item.observation_id in self._claimed:
            return False
        self._claimed[item.observation_id] = time.time()
        item.state = OBSERVATION_DISPATCHED
        return True

    def _recover_dispatched(self, timeout_seconds: int) -> None:
        cutoff = time.time() - timeout_seconds
        for item in list(self._observations.values()):
            if item.state != OBSERVATION_DISPATCHED:
                continue
            claimed_at = self._claimed.get(item.observation_id)
            if claimed_at is not None and claimed_at <= cutoff:
                item.state = OBSERVATION_PENDING
                self._claimed.pop(item.observation_id, None)

    def mark_done(self, observation_id: str) -> None:
        observation = self._observations.get(observation_id)
        if observation is not None:
            observation.state = OBSERVATION_DONE
            self._claimed.pop(observation_id, None)

    def count(self) -> int:
        return len(self._observations)


class PostgresActionObservationStore:
    """Durable observation store over the shared Runtime PostgreSQL bind."""

    TABLE_NAME = "agent_action_observations"

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        self._table = self._build_table()
        if create_tables:
            self._table.create(self._bind, checkfirst=True)
            if getattr(bind, "dialect", None) is not None and bind.dialect.name == "postgresql":
                with bind.begin() as connection:
                    connection.exec_driver_sql(
                        "ALTER TABLE agent_action_observations "
                        "ADD COLUMN IF NOT EXISTS run_id VARCHAR(128) NOT NULL DEFAULT ''"
                    )

    @classmethod
    def _build_table(cls) -> sa.Table:
        return sa.Table(
            cls.TABLE_NAME,
            sa.MetaData(),
            sa.Column("observation_id", sa.Text, primary_key=True),
            sa.Column("execution_id", sa.Text, nullable=False, unique=True),
            sa.Column("task_id", sa.Text, nullable=False, default=""),
            sa.Column("conversation_id", sa.Text, nullable=False, default=""),
            sa.Column("run_id", sa.Text, nullable=False, default=""),
            sa.Column("goal_id", sa.Text, nullable=False, default=""),
            sa.Column("capability", sa.Text, nullable=False, default=""),
            sa.Column("status", sa.Text, nullable=False, default=""),
            sa.Column("draft_id", sa.Text, nullable=False, default=""),
            sa.Column("schedule_id", sa.Text, nullable=False, default=""),
            sa.Column("artifact_refs", sa.JSON, nullable=False, default=list),
            sa.Column("resource_refs", sa.JSON, nullable=False, default=list),
            sa.Column("business_result", sa.JSON, nullable=False, default=dict),
            sa.Column("error", sa.Text, nullable=False, default=""),
            sa.Column("observed_at", sa.Text, nullable=False),
            sa.Column("state", sa.Text, nullable=False, default="PENDING"),
            sa.Column("payload", sa.JSON, nullable=False, default=dict),
            sa.Column("dispatched_at", sa.Text),
        )

    def save(self, observation: ActionObservation) -> ActionObservation:
        # Application-level idempotency by execution_id (the terminal hook may
        # run more than once through reconciliation); matches the memory store.
        existing = self.get_by_execution(observation.execution_id)
        if existing is not None:
            return existing
        with self._bind.begin() as connection:
            connection.execute(
                self._table.insert().values(
                    observation_id=observation.observation_id,
                    execution_id=observation.execution_id,
                    task_id=observation.task_id,
                    conversation_id=observation.conversation_id,
                    run_id=observation.run_id,
                    goal_id=observation.goal_id,
                    capability=observation.capability,
                    status=observation.status,
                    draft_id=observation.draft_id,
                    schedule_id=observation.schedule_id,
                    artifact_refs=list(observation.artifact_refs),
                    resource_refs=list(observation.resource_refs),
                    business_result=dict(observation.business_result),
                    error=observation.error,
                    observed_at=observation.observed_at,
                    state=observation.state,
                    payload=dict(observation.payload),
                )
            )
        return observation

    def get_by_execution(self, execution_id: str) -> ActionObservation | None:
        with self._bind.connect() as connection:
            row = connection.execute(
                sa.select(self._table).where(self._table.c.execution_id == execution_id)
            ).mappings().first()
        return _row_to_observation(row) if row else None

    def list_recent_for_tasks(
        self,
        task_ids: list[str] | tuple[str, ...] | set[str],
        *,
        limit: int = 8,
    ) -> list[ActionObservation]:
        """Read only recent receipts belonging to the current Task scope."""

        wanted = [str(item) for item in task_ids if str(item or "")]
        if not wanted or limit <= 0:
            return []
        with self._bind.connect() as connection:
            rows = connection.execute(
                sa.select(self._table)
                .where(self._table.c.task_id.in_(wanted))
                .order_by(self._table.c.observed_at.desc())
                .limit(max(1, limit))
            ).mappings().all()
        return [_row_to_observation(row) for row in rows]

    def list_pending(self) -> list[ActionObservation]:
        with self._bind.connect() as connection:
            rows = connection.execute(
                sa.select(self._table)
                .where(self._table.c.state == "PENDING")
                .order_by(self._table.c.observed_at)
            ).mappings().all()
        return [_row_to_observation(row) for row in rows]

    def claim_pending(
        self,
        batch_size: int = 1,
        dispatch_timeout_seconds: int = 600,
    ) -> list[ActionObservation]:
        # A DISPATCHED observation older than the timeout is treated as a
        # crashed consumer and returned to PENDING, so a restart can retry the
        # same progression.  Concurrent callers still rendezvous on the
        # single predecessor claim instead of submitting two Executions.
        cutoff_iso = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - max(0, dispatch_timeout_seconds),
            tz=UTC,
        ).isoformat()
        with self._bind.begin() as connection:
            connection.exec_driver_sql(
                f"""
                UPDATE {self.TABLE_NAME}
                SET state = 'PENDING', dispatched_at = NULL
                WHERE state = 'DISPATCHED'
                  AND dispatched_at IS NOT NULL
                  AND dispatched_at <= %s
                """,
                (cutoff_iso,),
            )
        with self._bind.begin() as connection:
            rows = connection.exec_driver_sql(
                f"""
                UPDATE {self.TABLE_NAME}
                SET state = 'DISPATCHED', dispatched_at = %s
                WHERE observation_id IN (
                    SELECT observation_id
                    FROM {self.TABLE_NAME}
                    WHERE state = 'PENDING'
                    ORDER BY observed_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING *
                """,
                (datetime.now(UTC).isoformat(), max(1, batch_size)),
        ).mappings().all()
        return [_row_to_observation(row) for row in rows]

    def claim_continuation(
        self,
        execution_id: str,
        dispatch_timeout_seconds: int = 600,
    ) -> ActionObservation | None:
        """Atomically claim one observation by its verified predecessor.

        The UPDATE is the cross-process CAS.  A completion callback and the
        polling consumer can therefore race safely: only one receives the
        row, while the loser submits no continuation.
        """

        if not str(execution_id or "").strip():
            return None
        cutoff_iso = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - max(0, dispatch_timeout_seconds),
            tz=UTC,
        ).isoformat()
        with self._bind.begin() as connection:
            connection.exec_driver_sql(
                f"""
                UPDATE {self.TABLE_NAME}
                SET state = 'PENDING', dispatched_at = NULL
                WHERE state = 'DISPATCHED'
                  AND dispatched_at IS NOT NULL
                  AND dispatched_at <= %s
                """,
                (cutoff_iso,),
            )
        with self._bind.begin() as connection:
            row = connection.exec_driver_sql(
                f"""
                UPDATE {self.TABLE_NAME}
                SET state = 'DISPATCHED', dispatched_at = %s
                WHERE execution_id = %s
                  AND state = 'PENDING'
                RETURNING *
                """,
                (datetime.now(UTC).isoformat(), str(execution_id)),
            ).mappings().first()
        return _row_to_observation(row) if row else None

    def mark_done(self, observation_id: str) -> None:
        with self._bind.begin() as connection:
            connection.exec_driver_sql(
                f"UPDATE {self.TABLE_NAME} SET state = 'DONE' WHERE observation_id = %s",
                (observation_id,),
            )

    def count(self) -> int:
        with self._bind.connect() as connection:
            row = connection.exec_driver_sql(
                f"SELECT COUNT(*) AS count FROM {self.TABLE_NAME}"
            ).mappings().first()
        return int(row["count"]) if row else 0


def _row_to_observation(row: Any) -> ActionObservation:
    return ActionObservation(
        observation_id=str(row["observation_id"]),
        execution_id=str(row["execution_id"]),
        task_id=str(row["task_id"] or ""),
        conversation_id=str(row["conversation_id"] or ""),
        run_id=str(row["run_id"] or ""),
        goal_id=str(row["goal_id"] or ""),
        capability=str(row["capability"] or ""),
        status=str(row["status"] or ""),
        draft_id=str(row["draft_id"] or ""),
        schedule_id=str(row["schedule_id"] or ""),
        artifact_refs=list(row["artifact_refs"] or []),
        resource_refs=list(row["resource_refs"] or []),
        business_result=dict(row["business_result"] or {}),
        error=str(row["error"] or ""),
        observed_at=str(row["observed_at"] or ""),
        state=str(row["state"] or OBSERVATION_PENDING),
        payload=dict(row["payload"] or {}),
    )


@dataclass
class ActionObservationWriter:
    """Persist one observation for a terminal incremental Execution.

    Called after the durable completion projection commits, so business
    resources (Draft artifact, Schedule) are already durable before the
    observation record exists.
    """

    store: ActionObservationStore | PostgresActionObservationStore
    artifact_store: ArtifactStore | None = None
    result_resolver: ResultResolver | None = None
    on_saved: Callable[..., Any] | None = None

    def _resolve(self, result: RuntimeResult, execution: Any | None = None) -> RuntimeResult:
        resolver = self.result_resolver or ResultResolver(
            artifact_store=self.artifact_store,
        )
        return resolver.resolve(result, execution=execution)

    def write(
        self,
        message: ExecutionQueueMessage,
        result: RuntimeResult,
        auth: AuthContext,
        *,
        execution: Any | None = None,
    ) -> ActionObservation | None:
        """Persist an observation only for INCREMENTAL executions."""
        resolved = self._resolve(result, execution=execution)
        if str(resolved.status or result.status or "").upper() not in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }:
            # WAITING_APPROVAL / WAITING_HUMAN is a durable pause, not
            # evidence for another AgentLoop turn.  Approval resumption owns
            # the next continuation explicitly.
            return None
        execution_input = message.payload.get("execution_input") or {}
        metadata = execution_input.get("execution_metadata") or {}
        if metadata.get("plan_mode") != "INCREMENTAL":
            return None
        steps = list(execution_input.get("steps") or [])
        first_step = steps[0] if steps else {}
        goal_id = str(
            execution_input.get("goal_id")
            or first_step.get("goal_id")
            or ""
        )
        capability = str(
            first_step.get("capability")
            or metadata.get("capability")
            or ""
        )
        task_id = str(
            message.payload.get("task_id")
            or execution_input.get("task_id")
            or ""
        )
        conversation_id = str(message.payload.get("conversation_id") or "")
        artifacts = list(resolved.artifacts or [])
        observation = ActionObservation(
            execution_id=str(resolved.execution_id or message.execution_id),
            task_id=task_id,
            conversation_id=conversation_id,
            run_id=str(message.payload.get("run_id") or ""),
            goal_id=goal_id,
            capability=capability,
            status=str(resolved.status or result.status or "COMPLETED"),
            draft_id=str(resolved.draft_id or ""),
            schedule_id=str(resolved.schedule_id or ""),
            artifact_refs=[
                str(item.get("artifact_id") or "")
                for item in artifacts
                if item.get("artifact_id")
            ],
            resource_refs=[
                {
                    key: item.get(key)
                    for key in ("resource_type", "resource_id", "artifact_id", "step_id")
                    if item.get(key) not in (None, "")
                }
                for item in artifacts
            ],
            business_result={
                "draft_id": str(resolved.draft_id or ""),
                "schedule_id": str(resolved.schedule_id or ""),
                "schedule": resolved.schedule or None,
                "artifact_ids": list(resolved.artifact_ids or []),
                "summary": str(resolved.summary or "")[:500],
            },
            error=str(
                result.error_message
                or result.error
                or resolved.error_message
                or ""
            ),
            payload={
                "goal_tree": metadata.get("goal_tree") or {},
                "command": metadata.get("command") or {},
                "session": message.payload.get("session") or {},
                "task_context": message.payload.get("task_context") or {},
            },
        )
        return self.store.save(observation)

    async def __call__(
        self,
        message: ExecutionQueueMessage,
        result: RuntimeResult,
        auth: AuthContext,
        *,
        execution: Any | None = None,
    ) -> ActionObservation | None:
        observation = self.write(message, result, auth, execution=execution)
        if observation is not None and self.on_saved is not None:
            try:
                callback_result = self.on_saved(
                    observation=observation,
                    message=message,
                    result=result,
                    auth=auth,
                    execution=execution,
                )
                if inspect.isawaitable(callback_result):
                    await callback_result
            except Exception:  # noqa: BLE001 - Memory must not break Runtime completion
                logger.warning(
                    "Post-observation Memory projection failed execution_id=%s",
                    observation.execution_id,
                    exc_info=True,
                )
        return observation


__all__ = [
    "ActionObservation",
    "ActionObservationStore",
    "ActionObservationWriter",
    "INCREMENTAL_PLAN_SOURCE",
    "OBSERVATION_DISPATCHED",
    "OBSERVATION_DONE",
    "OBSERVATION_PENDING",
    "PostgresActionObservationStore",
]
