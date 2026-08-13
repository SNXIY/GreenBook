"""Independent Artifact lifecycle registry."""

from __future__ import annotations

from collections.abc import Sequence

from .lifecycle import ArtifactLifecycleError, ArtifactLifecycleValidator
from .models import Artifact, ArtifactLifecycle, ArtifactReference
from .repository import ArtifactRepository
from .schema import ArtifactSchemaRegistry
from .store import ArtifactStore, ArtifactStorePort, MemoryArtifactStore


class ArtifactRegistryError(ValueError):
    pass


class ArtifactRegistry:
    """Artifact identity/lifecycle boundary, separate from execution state."""

    def __init__(
        self,
        store: ArtifactStorePort | ArtifactRepository | None = None,
        *,
        repository: ArtifactRepository | None = None,
        schema_registry: ArtifactSchemaRegistry | None = None,
    ) -> None:
        # ``repository=`` and positional ArtifactRepository preserve Phase15-D
        # compatibility; new callers inject a Store implementation instead.
        selected = repository or store
        if isinstance(selected, ArtifactRepository):
            self._store: ArtifactStorePort = MemoryArtifactStore(selected)
        elif selected is None:
            self._store = ArtifactStore()
        else:
            self._store = selected  # type: ignore[assignment]
        self._schemas = schema_registry or ArtifactSchemaRegistry()

    @property
    def schema_registry(self) -> ArtifactSchemaRegistry:
        """Expose the Container-owned schema registry to plugin runtimes."""

        return self._schemas

    def register(self, artifact: Artifact) -> Artifact:
        normalized = artifact.model_copy(deep=True)
        if not normalized.artifact_id or not normalized.artifact_type:
            raise ArtifactRegistryError("ARTIFACT_ID_AND_TYPE_REQUIRED")
        normalized.owner_task_id = normalized.owner_task_id or normalized.task_id
        normalized.owner_execution_id = normalized.owner_execution_id or normalized.execution_id
        existing = self._store.get(normalized.artifact_id)
        if existing is not None:
            if existing.model_dump(mode="json") != normalized.model_dump(mode="json"):
                raise ArtifactRegistryError("ARTIFACT_ID_CONFLICT")
            return existing
        return self._store.create(normalized)

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
        return self._store.get(artifact_id)

    def require(self, artifact_id: str) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise ArtifactRegistryError("ARTIFACT_NOT_FOUND")
        return artifact

    def find_by_task(self, task_id: str) -> list[Artifact]:
        return self._store.find_by_task(task_id)

    def find_by_ids(self, artifact_ids: Sequence[str]) -> list[Artifact]:
        return [artifact for artifact_id in artifact_ids if (artifact := self.get(artifact_id))]

    def mark_available(self, artifact_id: str) -> Artifact:
        return self._transition(artifact_id, ArtifactLifecycle.AVAILABLE)

    def mark_consumed(self, artifact_id: str, *, consumer_task_id: str = "") -> Artifact:
        artifact = self.require(artifact_id)
        if artifact.lifecycle == ArtifactLifecycle.ARCHIVED:
            raise ArtifactRegistryError("ARTIFACT_ALREADY_ARCHIVED")
        try:
            ArtifactLifecycleValidator.validate_input(artifact)
            ArtifactLifecycleValidator.validate_transition(
                artifact.lifecycle, ArtifactLifecycle.CONSUMED,
            )
        except ArtifactLifecycleError as exc:
            raise ArtifactRegistryError(str(exc)) from exc
        if consumer_task_id and consumer_task_id not in artifact.consumed_by_task_ids:
            artifact.consumed_by_task_ids.append(consumer_task_id)
        artifact.lifecycle = ArtifactLifecycle.CONSUMED
        return self._store.mark_consumed(artifact_id, consumer_task_id)

    def archive(self, artifact_id: str) -> Artifact:
        return self._transition(artifact_id, ArtifactLifecycle.ARCHIVED)

    def fail(self, artifact_id: str) -> Artifact:
        return self._transition(artifact_id, ArtifactLifecycle.FAILED)

    def _transition(self, artifact_id: str, lifecycle: ArtifactLifecycle) -> Artifact:
        artifact = self.require(artifact_id)
        if artifact.lifecycle == ArtifactLifecycle.ARCHIVED:
            raise ArtifactRegistryError("ARTIFACT_ALREADY_ARCHIVED")
        try:
            ArtifactLifecycleValidator.validate_transition(artifact.lifecycle, lifecycle)
        except ArtifactLifecycleError as exc:
            raise ArtifactRegistryError(str(exc)) from exc
        return self._store.update_status(artifact_id, lifecycle)


__all__ = ["ArtifactRegistry", "ArtifactRegistryError"]
