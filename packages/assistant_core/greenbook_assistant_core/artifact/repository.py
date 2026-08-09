"""ArtifactRepository — in-memory persistence for Artifacts.

Phase 4.2: in-memory store.  Phase 5+ migrates to PostgreSQL.
"""

from __future__ import annotations

from .models import Artifact

_store: dict[str, Artifact] = {}


class ArtifactRepository:
    """CRUD for Artifacts."""

    def save(self, artifact: Artifact) -> Artifact:
        _store[artifact.artifact_id] = artifact
        return artifact

    def find_by_id(self, artifact_id: str) -> Artifact | None:
        a = _store.get(artifact_id)
        return a.model_copy(deep=True) if a else None

    def find_by_execution(self, execution_id: str) -> list[Artifact]:
        return [
            a.model_copy(deep=True)
            for a in _store.values()
            if a.execution_id == execution_id
        ]

    def find_by_task(self, task_id: str) -> list[Artifact]:
        return [
            a.model_copy(deep=True)
            for a in _store.values()
            if a.task_id == task_id
        ]

    def find_by_step(self, step_id: str) -> Artifact | None:
        for a in _store.values():
            if a.step_id == step_id:
                return a.model_copy(deep=True)
        return None

    def find_by_type(
        self, execution_id: str, artifact_type: str,
    ) -> list[Artifact]:
        return [
            a.model_copy(deep=True)
            for a in _store.values()
            if a.execution_id == execution_id and a.artifact_type == artifact_type
        ]

    def delete_by_execution(self, execution_id: str) -> int:
        before = len(_store)
        keys = [k for k, a in _store.items() if a.execution_id == execution_id]
        for k in keys:
            del _store[k]
        return before - len(_store)

    @staticmethod
    def clear() -> None:
        _store.clear()
