"""Phase16-A Runtime composition and durable Artifact boundary tests."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from greenbook_agent_api.services.conversation_runtime_adapter import (
    ConversationRuntimeAdapter,
)
from greenbook_agent_api.services.runtime_agent_service import RuntimeAgentService
from greenbook_agent_core.artifact.models import Artifact, ArtifactLifecycle
from greenbook_agent_core.artifact.store import PostgresArtifactStore
from greenbook_agent_core.execution.execution_queue_worker import ExecutionQueueWorker
from greenbook_agent_core.execution.persistence_provider import RuntimePersistenceFactory
from greenbook_agent_core.goal.models import Goal, GoalTree
from greenbook_agent_core.planning.contracts import PlanStep, TaskPlan
from greenbook_agent_core.runtime.container import RuntimeContainer
from greenbook_agent_core.task.provider import TaskProviderError


def test_api_and_worker_use_the_same_persistence_profile_and_artifact_provider() -> None:
    """Separate processes compose the same durable provider from one profile."""

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    api_persistence = RuntimePersistenceFactory.from_env(
        storage="postgres",
        bind=engine,
        create_tables=True,
    )
    worker_persistence = RuntimePersistenceFactory.from_env(
        storage="postgres",
        bind=engine,
        create_tables=False,
    )
    api = RuntimeContainer.from_persistence(api_persistence)
    worker = RuntimeContainer.from_persistence(worker_persistence)

    assert api.persistence.storage == worker.persistence.storage == "postgres"
    assert isinstance(api.artifact_store, PostgresArtifactStore)
    assert type(api.artifact_store) is type(worker.artifact_store)
    assert api.artifact_registry._store is api.artifact_store
    assert worker.artifact_registry._store is worker.artifact_store

    api.close()
    worker.close()
    engine.dispose()


def test_artifact_created_by_api_is_consumed_after_worker_restart() -> None:
    """A new worker process can recover and advance an API-created Artifact."""

    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    first = RuntimePersistenceFactory.from_env(
        storage="postgres",
        bind=engine,
        create_tables=True,
    )
    api = RuntimeContainer.from_persistence(first)
    artifact = Artifact(
        artifact_id="phase16a-analysis",
        artifact_type="POST_ANALYSIS",
        task_id="task-analysis",
        execution_id="execution-phase16a",
        created_by_agent="AnalyticsAgent",
        metadata_schema="POST_ANALYSIS_SCHEMA",
        metadata={"summary": "analysis"},
    )
    api.artifact_registry.register(artifact)
    api.artifact_registry.mark_available(artifact.artifact_id)
    api.close()

    restarted_persistence = RuntimePersistenceFactory.from_env(
        storage="postgres",
        bind=engine,
        create_tables=False,
    )
    worker = RuntimeContainer.from_persistence(restarted_persistence)
    restored = worker.artifact_registry.require(artifact.artifact_id)
    assert restored.lifecycle == ArtifactLifecycle.AVAILABLE

    consumed = worker.artifact_registry.mark_consumed(
        artifact.artifact_id,
        consumer_task_id="task-creator",
    )
    assert consumed.lifecycle == ArtifactLifecycle.CONSUMED
    assert consumed.consumed_by_task_ids == ["task-creator"]

    worker.close()
    engine.dispose()


def test_runtime_modules_share_container_registries_without_recomposition() -> None:
    container = RuntimeContainer.for_testing()
    service = RuntimeAgentService(container=container)
    adapter = ConversationRuntimeAdapter(
        runtime_service=service,
        container=container,
    )

    assert service.container is container
    assert service._artifact_store is container.artifact_store
    assert service._registry is container.capability_registry
    assert not hasattr(service, "_orchestrator")
    assert adapter._artifact_registry is container.artifact_registry
    assert adapter._artifact_registry._schemas is container.artifact_schema_registry

    container.close()


def test_plan_submission_rejects_missing_executable_goal_coverage() -> None:
    tree = GoalTree(
        root=Goal(
            goal_id="root",
            description="Multiple independent goals",
            children=[
                Goal(goal_id="goal-a", description="First"),
                Goal(goal_id="goal-b", description="Second"),
                Goal(goal_id="goal-c", description="Third"),
            ],
        )
    )
    partial_plan = TaskPlan(
        task_id="coverage-test",
        steps=[
            PlanStep(
                step_id="goal-a:1",
                ordinal=1,
                capability="GENERATE_CONTENT",
                goal_id="goal-a",
            )
        ],
    )

    with pytest.raises(TaskProviderError) as exc_info:
        ConversationRuntimeAdapter._require_plan_goal_coverage(partial_plan, tree)

    assert exc_info.value.code == "PLAN_GOAL_COVERAGE_REQUIRED"


@pytest.mark.asyncio
async def test_queue_execution_still_consumes_the_container_queue() -> None:
    container = RuntimeContainer.for_testing()
    queue = container.persistence.execution_queue
    seen: list[str] = []

    async def handler(message) -> None:
        seen.append(message.execution_id)

    queue.enqueue("execution-phase16a", payload={"task_id": "task-phase16a"})
    worker = ExecutionQueueWorker(
        queue=queue,
        execution_handler=handler,
        worker_id="phase16a-worker",
        lease_seconds=30,
    )

    handled = await worker.run_once()

    assert len(handled) == 1
    assert seen == ["execution-phase16a"]
    assert queue.get_by_execution_id("execution-phase16a").status.value == "ACKED"
    container.close()
