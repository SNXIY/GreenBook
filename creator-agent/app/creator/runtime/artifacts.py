from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from app.creator.domain.errors import CreatorArtifactConflictError
from app.creator.runtime.models import (
    ArtifactKind,
    ArtifactPayload,
    ArtifactRef,
    CreatorArtifact,
    RunIdentity,
    utc_now,
)


def build_artifact(
    *,
    identity: RunIdentity,
    step_id: str,
    producer: str,
    revision: int,
    payload: ArtifactPayload,
    created_at: datetime | None = None,
) -> CreatorArtifact:
    canonical = _canonical_json(
        {
            "kind": payload.kind.value,
            "content": payload.content,
            "parent_ids": payload.parent_ids,
            "metadata": payload.metadata,
            "confidence": payload.confidence,
        }
    )
    content_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    identity_seed = ":".join(
        (
            identity.tenant_id,
            identity.task_id,
            identity.run_id,
            step_id,
            producer,
            payload.kind.value,
            str(revision),
            content_sha256,
        )
    )
    artifact_id = f"art_{hashlib.sha256(identity_seed.encode('utf-8')).hexdigest()}"
    return CreatorArtifact(
        id=artifact_id,
        tenant_id=identity.tenant_id,
        creator_id=identity.creator_id,
        task_id=identity.task_id,
        run_id=identity.run_id,
        step_id=step_id,
        kind=payload.kind,
        producer=producer,
        revision=revision,
        content=payload.content,
        parent_ids=payload.parent_ids,
        metadata=payload.metadata,
        confidence=payload.confidence,
        content_sha256=content_sha256,
        created_at=created_at or utc_now(),
    )


def next_artifact_revision(refs: Iterable[ArtifactRef], kind: ArtifactKind) -> int:
    return max((ref.revision for ref in refs if ref.kind == kind), default=0) + 1


class InMemoryCreatorArtifactStore:
    """Concurrency-safe Artifact Store used by unit tests and local composition."""

    def __init__(self) -> None:
        self._items: dict[str, CreatorArtifact] = {}
        self._lock = asyncio.Lock()

    async def put(self, artifact: CreatorArtifact) -> None:
        async with self._lock:
            existing = self._items.get(artifact.id)
            if existing is not None:
                _assert_same_artifact(existing, artifact)
                return
            self._items[artifact.id] = artifact

    async def get(self, artifact_id: str) -> CreatorArtifact | None:
        async with self._lock:
            return self._items.get(artifact_id)

    async def get_many(
        self, artifact_ids: tuple[str, ...]
    ) -> tuple[CreatorArtifact, ...]:
        async with self._lock:
            return tuple(
                self._items[artifact_id]
                for artifact_id in artifact_ids
                if artifact_id in self._items
            )

    async def list_for_run(self, run_id: str) -> tuple[CreatorArtifact, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (
                        artifact
                        for artifact in self._items.values()
                        if artifact.run_id == run_id
                    ),
                    key=lambda artifact: (artifact.created_at, artifact.id),
                )
            )


def assert_complete_artifact_load(
    requested_ids: tuple[str, ...],
    artifacts: tuple[CreatorArtifact, ...],
) -> None:
    loaded_ids = {artifact.id for artifact in artifacts}
    missing = set(requested_ids) - loaded_ids
    if missing:
        raise KeyError(f"Artifact Store is missing IDs: {sorted(missing)}")


def _assert_same_artifact(existing: CreatorArtifact, incoming: CreatorArtifact) -> None:
    stable_fields = (
        "tenant_id",
        "creator_id",
        "task_id",
        "run_id",
        "step_id",
        "kind",
        "producer",
        "revision",
        "content",
        "parent_ids",
        "metadata",
        "confidence",
        "content_sha256",
    )
    if any(
        getattr(existing, field) != getattr(incoming, field) for field in stable_fields
    ):
        raise CreatorArtifactConflictError(
            f"Artifact {incoming.id} cannot be overwritten",
            details={"artifact_id": incoming.id},
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
