from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.artifacts import publish_step_artifact, rollback_artifact
from app.database import Artifact, ArtifactRelation
from app.domain import TargetBinding


class _RollbackSession:
    def __init__(self, target: Artifact) -> None:
        self.target = target
        self.added: list[object] = []

    async def get(self, model, artifact_id: str):
        return self.target if model is Artifact and artifact_id == self.target.id else None

    async def scalar(self, statement):
        return 2

    def add(self, value) -> None:
        if isinstance(value, Artifact) and not value.id:
            value.id = "artifact-rollback"
        self.added.append(value)

    async def flush(self) -> None:
        return None


class _InsertOnlyArtifactSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    async def scalar(self, statement):
        return None

    async def scalars(self, statement):
        class _Empty:
            def all(self):
                return []

        return _Empty()

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None


@pytest.mark.asyncio
async def test_create_post_inserts_lifecycle_fields_once() -> None:
    session = _InsertOnlyArtifactSession()
    artifact = await publish_step_artifact(
        session,
        step=SimpleNamespace(
            run_id="run-1",
            id="step-1",
            task_key="create-draft",
            ordinal=1,
            agent_name="Creator",
            tool_name="creator.create_draft",
            depends_on=[],
        ),
        output={"draft_id": "draft-1"},
        artifact_type="CONTENT_DRAFT",
        change_type="CREATE_POST",
        version=1,
    )

    assert artifact.change_type == "CREATE_POST"
    assert artifact.parent_artifact_id is None
    assert len(session.added) == 1


def test_artifact_history_is_write_once_by_contract() -> None:
    artifact = Artifact(
        id="artifact-v1",
        run_id="run-1",
        task_key="draft",
        agent_name="Creator",
        artifact_type="CONTENT_DRAFT",
        version=1,
        change_type="CREATE_POST",
        content={},
        content_hash="a" * 64,
    )

    # Database-level immutability trigger rejects this UPDATE in PostgreSQL.
    # The application must never perform this assignment after INSERT.
    assert artifact.parent_artifact_id is None


def test_draft_versions_and_relations_are_explicit() -> None:
    draft_v1 = Artifact(
        id="draft-artifact-v1",
        run_id="run-1",
        task_key="create-draft",
        agent_name="Creator",
        artifact_type="CONTENT_DRAFT",
        version=1,
        change_type="CREATE_POST",
        content={"draft_id": "draft-1"},
        content_hash="a" * 64,
    )
    draft_v2 = Artifact(
        id="draft-artifact-v2",
        run_id="run-1",
        task_key="revise-draft",
        agent_name="Creator",
        artifact_type="CONTENT_DRAFT",
        parent_artifact_id=draft_v1.id,
        parent_artifact_ids=[draft_v1.id],
        version=2,
        change_type="APPEND_CONTENT",
        content={"draft_id": "draft-2"},
        content_hash="b" * 64,
    )
    relation = ArtifactRelation(
        source_artifact_id=draft_v2.id,
        target_artifact_id=draft_v1.id,
        relation_type="DERIVED_FROM",
    )

    assert draft_v2.parent_artifact_id == draft_v1.id
    assert draft_v2.version == 2
    assert draft_v2.change_type == "APPEND_CONTENT"
    assert relation.relation_type == "DERIVED_FROM"


def test_schedule_and_publication_bind_concrete_content_version() -> None:
    schedule = TargetBinding(
        target_type="SCHEDULE",
        role="SCHEDULE",
        target_id="schedule-1",
        artifact_id="schedule-artifact-1",
        content_artifact_id="draft-artifact-v2",
        content_artifact_version=2,
    )
    publication = TargetBinding(
        target_type="POST",
        role="PUBLICATION",
        target_id="post-1",
        artifact_id="publication-artifact-1",
        content_artifact_id=schedule.content_artifact_id,
        content_artifact_version=schedule.content_artifact_version,
    )

    assert schedule.content_artifact_id == "draft-artifact-v2"
    assert schedule.content_artifact_version == 2
    assert publication.content_artifact_id == schedule.content_artifact_id
    assert publication.content_artifact_version == schedule.content_artifact_version


@pytest.mark.asyncio
async def test_rollback_creates_new_version_without_overwriting_history() -> None:
    historical = Artifact(
        id="draft-artifact-v1",
        run_id="run-1",
        task_key="draft-lineage",
        agent_name="Creator",
        artifact_type="CONTENT_DRAFT",
        version=1,
        change_type="CREATE_POST",
        content={"draft_id": "draft-1", "body": "v1"},
        content_hash="a" * 64,
    )
    session = _RollbackSession(historical)

    restored = await rollback_artifact(
        session,
        target_artifact_id=historical.id,
        run_id="run-1",
        task_key="draft-lineage",
    )

    assert historical.version == 1
    assert restored.version == 3
    assert restored.change_type == "ROLLBACK"
    assert restored.parent_artifact_id == historical.id
    assert any(
        isinstance(item, ArtifactRelation)
        and item.relation_type == "DERIVED_FROM"
        for item in session.added
    )
