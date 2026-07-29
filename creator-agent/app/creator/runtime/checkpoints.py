from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.creator.domain.models import (
    CreatorDecisionAction,
    CreatorDecisionKind,
    CreatorGoal,
    CreatorTaskKind,
)
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
            yield saver
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
