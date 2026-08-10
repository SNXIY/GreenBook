"""Phase15-E persistence, schema, lifecycle, and timeline regression tests."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from greenbook_assistant_core.artifact.events import ArtifactEventType
from greenbook_assistant_core.artifact.lifecycle import (
    ArtifactLifecycleError,
    ArtifactLifecycleValidator,
)
from greenbook_assistant_core.artifact.models import Artifact, ArtifactLifecycle
from greenbook_assistant_core.artifact.registry import ArtifactRegistry, ArtifactRegistryError
from greenbook_assistant_core.artifact.schema import (
    ArtifactSchemaRegistry,
    ArtifactSchemaValidationError,
)
from greenbook_assistant_core.artifact.store import (
    ArtifactStore,
    MemoryArtifactStore,
    PostgresArtifactStore,
)
from greenbook_assistant_core.artifact.repository import ArtifactRepository
from greenbook_assistant_core.execution.event_store import ExecutionEventStore
from greenbook_assistant_core.execution.timeline import ExecutionTimelineService, TimelineItemKind
from greenbook_assistant_core.orchestration.agent_registry import AgentRegistry


def _analysis_artifact() -> Artifact:
    return Artifact(
        artifact_id="analysis-java-1",
        artifact_type="POST_ANALYSIS",
        task_id="task-java-analysis",
        execution_id="execution-java-analysis",
        created_by_agent="AnalyticsAgent",
        metadata_schema="POST_ANALYSIS_SCHEMA",
        storage_type="postgres",
        location="artifact://analysis/java001",
        metadata={
            "posts": [{"id": "p1"}],
            "summary": "新人常见问题",
            "statistics": {"count": 1},
            "content": "must not be persisted",
        },
    )


def test_memory_store_create_get_update_and_recover_in_same_contract() -> None:
    store = MemoryArtifactStore()
    artifact = store.create(_analysis_artifact())
    assert store.get(artifact.artifact_id).owner_task_id == "task-java-analysis"
    available = store.update_status(artifact.artifact_id, ArtifactLifecycle.AVAILABLE)
    assert available.lifecycle == ArtifactLifecycle.AVAILABLE
    assert available.version == 2
    assert store.list_events(artifact.execution_id)[-1].event_type == ArtifactEventType.AVAILABLE


def test_postgres_store_metadata_and_restart_recovery() -> None:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    first = PostgresArtifactStore(engine)
    first.create(_analysis_artifact())
    first.update_status("analysis-java-1", ArtifactLifecycle.AVAILABLE)

    restarted = PostgresArtifactStore(engine, create_tables=False)
    restored = restarted.get("analysis-java-1")
    assert restored is not None
    assert restored.lifecycle == ArtifactLifecycle.AVAILABLE
    assert restored.location == "artifact://analysis/java001"
    assert restored.metadata["posts"] == [{"id": "p1"}]
    assert "content" not in restored.metadata
    events = restarted.list_events("execution-java-analysis")
    assert [event.event_type for event in events] == [
        ArtifactEventType.CREATED,
        ArtifactEventType.AVAILABLE,
    ]
    engine.dispose()


def test_lifecycle_validator_rejects_created_archived_and_duplicate_consumption() -> None:
    validator = ArtifactLifecycleValidator()
    with pytest.raises(ArtifactLifecycleError, match="NOT_AVAILABLE"):
        validator.validate_input(_analysis_artifact())
    validator.validate_transition(ArtifactLifecycle.CREATED, ArtifactLifecycle.AVAILABLE)
    with pytest.raises(ArtifactLifecycleError, match="INVALID_ARTIFACT_TRANSITION"):
        validator.validate_transition(ArtifactLifecycle.CREATED, ArtifactLifecycle.CONSUMED)

    ArtifactRepository.clear()
    registry = ArtifactRegistry(ArtifactStore())
    registry.register(_analysis_artifact())
    registry.mark_available("analysis-java-1")
    registry.mark_consumed("analysis-java-1", consumer_task_id="task-creator")
    with pytest.raises(ArtifactRegistryError, match="ALREADY_CONSUMED"):
        registry.mark_consumed("analysis-java-1", consumer_task_id="task-publish")


def test_schema_validation_and_agent_contracts_fail_before_execution() -> None:
    schemas = ArtifactSchemaRegistry()
    schemas.validate(_analysis_artifact())
    invalid = _analysis_artifact().model_copy(update={"metadata": {"posts": []}})
    with pytest.raises(ArtifactSchemaValidationError, match="REQUIRED_FIELDS_MISSING"):
        schemas.validate(invalid)

    agents = AgentRegistry()
    agents.validate_schema_contract("AnalyticsAgent", "CreatorAgent")
    agents.validate_schema_contract("CreatorAgent", "PublishAgent")
    with pytest.raises(Exception, match="PLAN_SCHEMA_CONTRACT"):
        agents.validate_schema_contract("SearchAgent", "PublishAgent")


def test_artifact_events_are_exposed_as_timeline_items() -> None:
    events = ExecutionEventStore()
    ArtifactRepository.clear()
    store = MemoryArtifactStore()
    artifact = store.create(_analysis_artifact())
    store.update_status(artifact.artifact_id, ArtifactLifecycle.AVAILABLE)
    store.mark_consumed(artifact.artifact_id, "task-creator")
    timeline = ExecutionTimelineService(events, artifact_store=store).build(
        "execution-java-analysis"
    )
    assert [item.kind for item in timeline.items] == [
        TimelineItemKind.ARTIFACT,
        TimelineItemKind.ARTIFACT,
        TimelineItemKind.ARTIFACT,
    ]
    assert any(item.event_type == "ARTIFACT_CONSUMED" for item in timeline.items)
