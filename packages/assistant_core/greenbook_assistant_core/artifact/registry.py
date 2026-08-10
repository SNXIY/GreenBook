"""Independent Artifact lifecycle registry."""

from __future__ import annotations

from collections.abc import Sequence

from .models import Artifact, ArtifactLifecycle, ArtifactReference
from .repository import ArtifactRepository


class ArtifactRegistryError(ValueError):
    pass


class ArtifactRegistry:
    """Artifact identity/lifecycle boundary, separate from execution state."""

    def __init__(self, repository: ArtifactRepository | None = None) -> None:
        self._repository = repository or ArtifactRepository()

    def register(self, artifact: Artifact) -> Artifact:
        normalized = artifact.model_copy(deep=True)
        if not normalized.artifact_id or not normalized.artifact_type:
            raise ArtifactRegistryError("ARTIFACT_ID_AND_TYPE_REQUIRED")
        normalized.owner_task_id = normalized.owner_task_id or normalized.task_id
        normalized.owner_execution_id = normalized.owner_execution_id or normalized.execution_id
        existing = self._repository.find_by_id(normalized.artifact_id)
        if existing is not None:
            if existing.model_dump(mode="json") != normalized.model_dump(mode="json"):
                raise ArtifactRegistryError("ARTIFACT_ID_CONFLICT")
            return existing
        return self._repository.save(normalized)

    def register_reference(
        self,
        reference: ArtifactReference,
        *,
        task_id: str = "",
        execution_id: str = "",
    ) -> Artifact:
        return self.register(Artifact(
            artifact_id=reference.artifact_id,
            artifact_type=reference.artifact_type,
            task_id=task_id or reference.owner_task_id,
            execution_id=execution_id or reference.owner_execution_id,
            owner_task_id=reference.owner_task_id,
            owner_execution_id=reference.owner_execution_id,
            created_by_agent=reference.created_by_agent,
            metadata_schema=reference.metadata_schema,
            version=reference.version,
            storage_type=reference.storage_type,
            location=reference.location,
            content_hash=reference.content_hash,
        ))

    def get(self, artifact_id: str) -> Artifact | None:
        return self._repository.find_by_id(artifact_id)

    def require(self, artifact_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise ArtifactRegistryError("ARTIFACT_NOT_FOUND")
        return artifact

    def find_by_task(self, task_id: str) -> list[Artifact]:
        return self._repository.find_by_task(task_id)

    def find_by_ids(self, artifact_ids: Sequence[str]) -> list[Artifact]:
        return [artifact for artifact_id in artifact_ids if (artifact := self.get(artifact_id))]

    def mark_available(self, artifact_id: str) -> Artifact:
        return self._transition(artifact_id, ArtifactLifecycle.AVAILABLE)

    def mark_consumed(self, artifact_id: str, *, consumer_task_id: str = "") -> Artifact:
        artifact = self.require(artifact_id)
        if artifact.lifecycle not in {ArtifactLifecycle.CREATED, ArtifactLifecycle.AVAILABLE}:
            raise ArtifactRegistryError("ARTIFACT_NOT_CONSUMABLE")
        if consumer_task_id and consumer_task_id not in artifact.consumed_by_task_ids:
            artifact.consumed_by_task_ids.append(consumer_task_id)
        artifact.lifecycle = ArtifactLifecycle.CONSUMED
        return self._repository.save(artifact)

    def archive(self, artifact_id: str) -> Artifact:
        return self._transition(artifact_id, ArtifactLifecycle.ARCHIVED)

    def fail(self, artifact_id: str) -> Artifact:
        return self._transition(artifact_id, ArtifactLifecycle.FAILED)

    def _transition(self, artifact_id: str, lifecycle: ArtifactLifecycle) -> Artifact:
        artifact = self.require(artifact_id)
        if artifact.lifecycle == ArtifactLifecycle.ARCHIVED:
            raise ArtifactRegistryError("ARTIFACT_ALREADY_ARCHIVED")
        artifact.lifecycle = lifecycle
        return self._repository.save(artifact)


__all__ = ["ArtifactRegistry", "ArtifactRegistryError"]
