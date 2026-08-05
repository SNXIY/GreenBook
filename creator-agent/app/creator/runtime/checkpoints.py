from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.memory import InMemorySaver

from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorGoal,
    CreatorTaskKind,
)


logger = logging.getLogger("uvicorn.error")


class DiagnosticCheckpointer(BaseCheckpointSaver):
    """Small async proxy used to time checkpoint boundaries in the harness."""

    def __init__(self, delegate: BaseCheckpointSaver, *, label: str):
        self._delegate = delegate
        self._label = label
        super().__init__(serde=delegate.serde)

    @property
    def serde(self):
        return self._delegate.serde

    @serde.setter
    def serde(self, value):
        self._delegate.serde = value

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def get_tuple(self, config):
        return self._delegate.get_tuple(config)

    def put(self, config, checkpoint, metadata, new_versions):
        return self._delegate.put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        return self._delegate.put_writes(config, writes, task_id, task_path)

    @staticmethod
    def _identity(config) -> dict[str, object]:
        configurable = dict(config.get("configurable", {}))
        return {
            "thread_id": configurable.get("thread_id"),
            "checkpoint_ns": configurable.get("checkpoint_ns", ""),
            "checkpoint_id": configurable.get("checkpoint_id"),
            "parent_checkpoint_id": configurable.get("checkpoint_id"),
        }

    async def aget_tuple(self, config):
        started = time.monotonic()
        logger.info("checkpoint_get_started label=%s identity=%s", self._label, self._identity(config))
        result = await asyncio.wait_for(self._delegate.aget_tuple(config), timeout=30.0)
        logger.info(
            "checkpoint_get_finished label=%s identity=%s duration_ms=%.1f found=%s",
            self._label, self._identity(config), (time.monotonic() - started) * 1000, result is not None,
        )
        return result

    async def aput(self, config, checkpoint, metadata, new_versions):
        started = time.monotonic()
        logger.info("checkpoint_put_started label=%s identity=%s", self._label, self._identity(config))
        try:
            result = await asyncio.wait_for(
                self._delegate.aput(config, checkpoint, metadata, new_versions),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.exception("checkpoint_put_timeout label=%s identity=%s", self._label, self._identity(config))
            raise
        logger.info(
            "checkpoint_put_finished label=%s identity=%s duration_ms=%.1f",
            self._label, self._identity(config), (time.monotonic() - started) * 1000,
        )
        return result

    async def aput_writes(self, config, writes, task_id, task_path=""):
        started = time.monotonic()
        logger.info("checkpoint_writes_started label=%s identity=%s task_id=%s", self._label, self._identity(config), task_id)
        result = await asyncio.wait_for(
            self._delegate.aput_writes(config, writes, task_id, task_path),
            timeout=30.0,
        )
        logger.info("checkpoint_writes_finished label=%s identity=%s duration_ms=%.1f", self._label, self._identity(config), (time.monotonic() - started) * 1000)
        return result
from app.creator.runtime.models import (
    AgentCapability,
    AgentUsage,
    ArtifactKind,
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    FactRecord,
    HumanDecisionRequest,
    PlanSnapshot,
    PlanStep,
    PlanStepStatus,
    ProgressEntry,
    RunIdentity,
    RuntimeControlStatus,
    RuntimeFailure,
    StepExecution,
    SupervisorAction,
    SupervisorDecision,
)


class CreatorCheckpointSettings(Protocol):
    creator_checkpoint_backend: str
    creator_checkpoint_sqlite_path: str
    creator_checkpoint_postgres_url: str
    creator_checkpoint_auto_setup: bool
    creator_checkpoint_diagnostics: bool


CREATOR_CHECKPOINT_TYPES = (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorGoal,
    CreatorTaskKind,
    AgentCapability,
    AgentUsage,
    ArtifactKind,
    ArtifactRef,
    BudgetLimits,
    BudgetUsage,
    FactRecord,
    HumanDecisionRequest,
    PlanSnapshot,
    PlanStep,
    PlanStepStatus,
    ProgressEntry,
    RunIdentity,
    RuntimeControlStatus,
    RuntimeFailure,
    StepExecution,
    SupervisorAction,
    SupervisorDecision,
)


def configure_creator_checkpointer(
    checkpointer: BaseCheckpointSaver,
) -> BaseCheckpointSaver:
    checkpointer.serde = JsonPlusSerializer(
        allowed_msgpack_modules=CREATOR_CHECKPOINT_TYPES
    )
    return checkpointer


@asynccontextmanager
async def open_creator_checkpointer(
    settings: CreatorCheckpointSettings,
    *,
    ensure_schema: bool | None = None,
) -> AsyncIterator[BaseCheckpointSaver]:
    should_setup = (
        settings.creator_checkpoint_auto_setup
        if ensure_schema is None
        else ensure_schema
    )
    backend = settings.creator_checkpoint_backend.strip().lower()
    if backend == "sqlite":
        path = Path(settings.creator_checkpoint_sqlite_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
            configure_creator_checkpointer(saver)
            if should_setup:
                await saver.setup()
            yield DiagnosticCheckpointer(saver, label=f"sqlite:{path.resolve()}") if settings.creator_checkpoint_diagnostics else saver
        return
    if backend == "memory":
        saver = InMemorySaver()
        configure_creator_checkpointer(saver)
        yield DiagnosticCheckpointer(saver, label="memory") if settings.creator_checkpoint_diagnostics else saver
        return
    if backend == "postgres":
        if not settings.creator_checkpoint_postgres_url:
            raise ValueError("CREATOR_CHECKPOINT_POSTGRES_URL is required for postgres")
        async with AsyncPostgresSaver.from_conn_string(
            settings.creator_checkpoint_postgres_url
        ) as saver:
            configure_creator_checkpointer(saver)
            if should_setup:
                await saver.setup()
            yield saver
        return
    raise ValueError("CREATOR_CHECKPOINT_BACKEND must be 'sqlite' or 'postgres'")
