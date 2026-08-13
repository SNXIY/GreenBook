"""Artifact storage contracts and memory/PostgreSQL implementations."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

import sqlalchemy as sa

from greenbook_agent_core.execution.invocation import ExecutionResult
from greenbook_agent_core.execution.models import StepExecution

from .events import ArtifactEventType, ArtifactLifecycleEvent
from .models import Artifact, ArtifactLifecycle
from .persistence import artifact_events, artifact_metadata, artifact_records
from .repository import ArtifactRepository

logger = logging.getLogger(__name__)


class ArtifactStorePort(Protocol):
    """Storage contract used by ArtifactRegistry."""

    def create(self, artifact: Artifact) -> Artifact: ...
    def get(self, artifact_id: str) -> Artifact | None: ...
    def update_status(self, artifact_id: str, status: Any) -> Artifact: ...
    def find_by_task(self, task_id: str) -> list[Artifact]: ...
    def find_by_execution(self, execution_id: str) -> list[Artifact]: ...
    def mark_consumed(self, artifact_id: str, consumer_task_id: str = "") -> Artifact: ...
    def archive(self, artifact_id: str) -> Artifact: ...


class ArtifactStore:
    """Memory-backed ArtifactStore kept as the compatibility default."""

    def __init__(self, repository: ArtifactRepository | None = None) -> None:
        self._repo = repository or ArtifactRepository()
        self._events: list[ArtifactLifecycleEvent] = []

    def create(self, artifact: Artifact) -> Artifact:
        normalized = artifact.model_copy(deep=True)
        normalized.owner_task_id = normalized.owner_task_id or normalized.task_id
        normalized.owner_execution_id = normalized.owner_execution_id or normalized.execution_id
        created = self._repo.find_by_id(normalized.artifact_id) is None
        saved = self._repo.save(normalized)
        if created:
            self._events.append(_event_for(saved, ArtifactEventType.CREATED))
        return saved

    def get(self, artifact_id: str) -> Artifact | None:
        return self._repo.find_by_id(artifact_id)

    def update_status(self, artifact_id: str, status: Any) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        artifact.lifecycle = status
        artifact.version += 1
        artifact.updated_at = datetime.now(UTC).isoformat()
        saved = self.create(artifact)
        self._events.append(_event_for(saved, _event_type_for(status)))
        return saved

    def mark_consumed(self, artifact_id: str, consumer_task_id: str = "") -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        if consumer_task_id and consumer_task_id not in artifact.consumed_by_task_ids:
            artifact.consumed_by_task_ids.append(consumer_task_id)
        artifact.lifecycle = "CONSUMED"
        artifact.version += 1
        artifact.updated_at = datetime.now(UTC).isoformat()
        saved = self.create(artifact)
        self._events.append(_event_for(saved, ArtifactEventType.CONSUMED))
        return saved

    def archive(self, artifact_id: str) -> Artifact:
        return self.update_status(artifact_id, "ARCHIVED")

    def create_from_result(
        self,
        result: ExecutionResult,
        *,
        task_id: str = "",
        execution_id: str = "",
        step_id: str = "",
    ) -> Artifact | None:
        """Create an Artifact from a successful step result."""
        if not result.ok or result.artifact is None or not result.artifact.artifact_type:
            return None
        projection = _safe_result_projection(result)
        artifact = Artifact(
            task_id=task_id,
            execution_id=execution_id,
            owner_task_id=task_id,
            owner_execution_id=execution_id,
            created_by_agent=result.capability or result.tool_name,
            step_id=step_id,
            artifact_type=result.artifact.artifact_type,
            resource_id=projection["resource_id"],
            resource_kind=projection["resource_type"],
            resource_type=projection["resource_type"],
            title=projection["title"],
            summary=projection["summary"],
            status=projection["status"],
            run_at=projection["run_at"],
            timezone=projection["timezone"],
            metadata={
                "tool_name": result.tool_name,
                "capability": result.capability,
                "projection": projection,
            },
            metadata_schema="greenbook.tool_result.v1",
        )
        return self.create(artifact)

    def resolve_inputs(self, step: StepExecution, execution_id: str) -> list[Artifact]:
        needed = step.input_artifact_types
        if not needed:
            return []
        all_artifacts = self.find_by_execution(execution_id)
        resolved: list[Artifact] = []
        for art_type in needed:
            candidates = [a for a in all_artifacts if a.artifact_type == art_type]
            if candidates:
                candidates.sort(key=lambda a: a.created_at, reverse=True)
                resolved.append(candidates[0])
            else:
                logger.debug("Artifact type '%s' not found for step %s", art_type, step.step_id)
        return resolved

    def resolve_for_step_type(self, execution_id: str, artifact_type: str) -> Artifact | None:
        candidates = [a for a in self.find_by_execution(execution_id)
                      if a.artifact_type == artifact_type]
        candidates.sort(key=lambda a: a.created_at, reverse=True)
        return candidates[0] if candidates else None

    def find_by_execution(self, execution_id: str) -> list[Artifact]:
        return self._repo.find_by_execution(execution_id)

    def find_by_task(self, task_id: str) -> list[Artifact]:
        return self._repo.find_by_task(task_id)

    def find_by_step(self, step_id: str) -> Artifact | None:
        return self._repo.find_by_step(step_id)

    def count(self, execution_id: str) -> int:
        return len(self.find_by_execution(execution_id))

    def list_events(self, execution_id: str) -> list[ArtifactLifecycleEvent]:
        return [event.model_copy(deep=True) for event in self._events
                if event.execution_id == execution_id]


class MemoryArtifactStore(ArtifactStore):
    """Explicit name for the process-local Store implementation."""


MemoryStore = MemoryArtifactStore
ArtifactStoreInterface = ArtifactStorePort


class PostgresArtifactStore:
    """Synchronous SQLAlchemy Store for metadata and storage references only."""

    def __init__(self, bind: Any, *, create_tables: bool = True) -> None:
        self._bind = bind
        if create_tables:
            artifact_metadata.create_all(bind)

    def _connect(self):
        if isinstance(self._bind, sa.engine.Connection):
            return _ConnectionContext(self._bind)
        return self._bind.begin()

    def create(self, artifact: Artifact) -> Artifact:
        now = datetime.now(UTC).isoformat()
        values = _record_values(artifact, updated_at=now)
        with self._connect() as conn:
            existing = conn.execute(
                sa.select(artifact_records.c.artifact_id).where(
                    artifact_records.c.artifact_id == artifact.artifact_id
                )
            ).first()
            if existing:
                conn.execute(
                    sa.update(artifact_records)
                    .where(artifact_records.c.artifact_id == artifact.artifact_id)
                    .values(**values)
                )
            else:
                conn.execute(sa.insert(artifact_records).values(**values))
                conn.execute(sa.insert(artifact_events).values(
                    _event_values(_event_for(artifact, ArtifactEventType.CREATED))
                ))
        return artifact.model_copy(deep=True)

    def create_from_result(
        self,
        result: ExecutionResult,
        *,
        task_id: str = "",
        execution_id: str = "",
        step_id: str = "",
    ) -> Artifact | None:
        if not result.ok or result.artifact is None or not result.artifact.artifact_type:
            return None
        projection = _safe_result_projection(result)
        return self.create(Artifact(
            task_id=task_id,
            execution_id=execution_id,
            owner_task_id=task_id,
            owner_execution_id=execution_id,
            created_by_agent=result.capability or result.tool_name,
            step_id=step_id,
            artifact_type=result.artifact.artifact_type,
            resource_id=projection["resource_id"],
            resource_kind=projection["resource_type"],
            resource_type=projection["resource_type"],
            title=projection["title"],
            summary=projection["summary"],
            status=projection["status"],
            run_at=projection["run_at"],
            timezone=projection["timezone"],
            metadata={
                "tool_name": result.tool_name,
                "capability": result.capability,
                "projection": projection,
            },
            metadata_schema="greenbook.tool_result.v1",
        ))

    def get(self, artifact_id: str) -> Artifact | None:
        with self._connect() as conn:
            row = conn.execute(
                sa.select(artifact_records).where(
                    artifact_records.c.artifact_id == artifact_id
                )
            ).mappings().first()
        return _artifact_from_row(row) if row else None

    def update_status(self, artifact_id: str, status: Any) -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        artifact.lifecycle = status
        artifact.version += 1
        artifact.updated_at = datetime.now(UTC).isoformat()
        saved = self.create(artifact)
        self._append_event(saved, _event_type_for(status))
        return saved

    def mark_consumed(self, artifact_id: str, consumer_task_id: str = "") -> Artifact:
        artifact = self.get(artifact_id)
        if artifact is None:
            raise KeyError(artifact_id)
        if consumer_task_id and consumer_task_id not in artifact.consumed_by_task_ids:
            artifact.consumed_by_task_ids.append(consumer_task_id)
        artifact.lifecycle = "CONSUMED"
        artifact.version += 1
        artifact.updated_at = datetime.now(UTC).isoformat()
        saved = self.create(artifact)
        self._append_event(saved, ArtifactEventType.CONSUMED)
        return saved

    def archive(self, artifact_id: str) -> Artifact:
        return self.update_status(artifact_id, "ARCHIVED")

    def find_by_task(self, task_id: str) -> list[Artifact]:
        return self._find("owner_task_id", task_id)

    def find_by_execution(self, execution_id: str) -> list[Artifact]:
        return self._find("owner_execution_id", execution_id)

    def resolve_inputs(self, step: StepExecution, execution_id: str) -> list[Artifact]:
        return [candidate for needed in step.input_artifact_types
                if (candidate := self.resolve_for_step_type(execution_id, needed))]

    def resolve_for_step_type(self, execution_id: str, artifact_type: str) -> Artifact | None:
        candidates = [a for a in self.find_by_execution(execution_id)
                      if a.artifact_type == artifact_type]
        candidates.sort(key=lambda a: a.created_at, reverse=True)
        return candidates[0] if candidates else None

    def count(self, execution_id: str) -> int:
        return len(self.find_by_execution(execution_id))

    def list_events(self, execution_id: str) -> list[ArtifactLifecycleEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(artifact_events)
                .where(artifact_events.c.execution_id == execution_id)
                .order_by(artifact_events.c.timestamp)
            ).mappings().all()
        return [_event_from_row(row) for row in rows]

    def _append_event(self, artifact: Artifact, event_type: ArtifactEventType) -> None:
        event = _event_for(artifact, event_type)
        with self._connect() as conn:
            conn.execute(sa.insert(artifact_events).values(_event_values(event)))

    def find_by_step(self, step_id: str) -> Artifact | None:
        return None

    def _find(self, column: str, value: str) -> list[Artifact]:
        with self._connect() as conn:
            rows = conn.execute(
                sa.select(artifact_records)
                .where(getattr(artifact_records.c, column) == value)
                .order_by(artifact_records.c.created_at)
            ).mappings().all()
        return [_artifact_from_row(row) for row in rows]


class _ConnectionContext:
    def __init__(self, connection: sa.engine.Connection) -> None:
        self._connection = connection

    def __enter__(self):
        return self._connection

    def __exit__(self, *_args) -> None:
        pass


def _record_values(artifact: Artifact, *, updated_at: str) -> dict[str, Any]:
    return {
        "artifact_id": artifact.artifact_id,
        "artifact_type": artifact.artifact_type,
        "resource_type": artifact.resource_type or artifact.resource_kind,
        "resource_id": artifact.resource_id,
        "title": artifact.title,
        "summary": artifact.summary,
        "result_status": artifact.status,
        "run_at": artifact.run_at,
        "timezone": artifact.timezone,
        "step_id": artifact.step_id,
        "owner_task_id": artifact.owner_task_id or artifact.task_id,
        "owner_execution_id": artifact.owner_execution_id or artifact.execution_id,
        "owner_agent": artifact.created_by_agent,
        "lifecycle": str(artifact.lifecycle),
        "schema_version": artifact.metadata_schema,
        "metadata_json": _compact_metadata(artifact.metadata),
        "storage_type": artifact.storage_type,
        "storage_reference": artifact.location,
        "content_hash": artifact.content_hash,
        "version": artifact.version,
        "size": artifact.size,
        "consumed_by_task_ids": list(artifact.consumed_by_task_ids),
        "created_at": artifact.created_at,
        "updated_at": artifact.updated_at or updated_at,
    }


def _event_for(artifact: Artifact, event_type: ArtifactEventType) -> ArtifactLifecycleEvent:
    return ArtifactLifecycleEvent(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        execution_id=artifact.execution_id or artifact.owner_execution_id,
        task_id=artifact.task_id or artifact.owner_task_id,
        agent_name=artifact.created_by_agent,
        event_type=event_type,
        lifecycle=str(artifact.lifecycle),
    )


def _event_type_for(status: Any) -> ArtifactEventType:
    lifecycle = str(status)
    return {
        str(ArtifactLifecycle.AVAILABLE): ArtifactEventType.AVAILABLE,
        str(ArtifactLifecycle.CONSUMED): ArtifactEventType.CONSUMED,
        str(ArtifactLifecycle.ARCHIVED): ArtifactEventType.ARCHIVED,
        str(ArtifactLifecycle.FAILED): ArtifactEventType.FAILED,
    }.get(lifecycle, ArtifactEventType.AVAILABLE)


def _event_values(event: ArtifactLifecycleEvent) -> dict[str, Any]:
    return event.model_dump(mode="json")


def _event_from_row(row: Any) -> ArtifactLifecycleEvent:
    data = dict(row)
    return ArtifactLifecycleEvent.model_validate(data)


def _artifact_from_row(row: Any) -> Artifact:
    data = dict(row)
    owner_task_id = data.get("owner_task_id") or ""
    owner_execution_id = data.get("owner_execution_id") or ""
    return Artifact(
        artifact_id=data["artifact_id"],
        artifact_type=data["artifact_type"],
        resource_type=data.get("resource_type"),
        resource_kind=data.get("resource_type"),
        resource_id=data.get("resource_id"),
        title=data.get("title"),
        summary=data.get("summary") or "",
        status=data.get("result_status"),
        run_at=data.get("run_at"),
        timezone=data.get("timezone"),
        step_id=data.get("step_id") or "",
        task_id=owner_task_id,
        execution_id=owner_execution_id,
        owner_task_id=owner_task_id,
        owner_execution_id=owner_execution_id,
        created_by_agent=data.get("owner_agent") or "",
        lifecycle=data.get("lifecycle") or "CREATED",
        metadata_schema=data.get("schema_version") or "",
        metadata=data.get("metadata_json") or {},
        storage_type=data.get("storage_type") or "INLINE",
        location=data.get("storage_reference"),
        content_hash=data.get("content_hash"),
        version=data.get("version") or 1,
        size=data.get("size"),
        consumed_by_task_ids=list(data.get("consumed_by_task_ids") or []),
        created_at=data.get("created_at") or datetime.now(UTC).isoformat(),
        updated_at=data.get("updated_at") or datetime.now(UTC).isoformat(),
    )


def _compact_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Keep database rows metadata-only and exclude common body fields."""
    blocked = {"body", "body_markdown", "content", "tool_result", "raw_text"}

    def compact(value: Any, key: str = "") -> Any:
        if key.lower() in blocked:
            return None
        if isinstance(value, dict):
            return {str(k): item for k, raw in value.items()
                    if (item := compact(raw, str(k))) is not None}
        if isinstance(value, list):
            return [item for raw in value[:32] if (item := compact(raw, key)) is not None]
        if isinstance(value, str):
            return value[:512]
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        return str(value)[:256]

    return compact(metadata) or {}


def _kind_from_type(artifact_type: str) -> str | None:
    return {
        "DRAFT": "DRAFT",
        "SEARCH_RESULT": "POST",
        "SCHEDULE": "SCHEDULE",
        "ANALYSIS_REPORT": "ARTIFACT",
        "VALIDATION_REPORT": "ARTIFACT",
        "PERFORMANCE_DATA": "ARTIFACT",
        "COMMENT": "COMMENT",
        "PUBLICATION": "POST",
    }.get(artifact_type)


def _safe_result_projection(result: ExecutionResult) -> dict[str, Any]:
    """Return the same body-free result fields for memory and PostgreSQL."""

    data = result.tool_result.get("data") if result.tool_result else None
    if not isinstance(data, dict):
        data = {}
    artifact_type = str(result.artifact.artifact_type).strip().upper()
    resource_key = _resource_key_for_type(artifact_type)
    resource_id = data.get(resource_key) if resource_key else result.artifact.resource_id
    resource_type = _kind_from_type(artifact_type)
    title = _optional_text(data.get("title"))
    summary = _optional_text(
        data.get("summary") or data.get("description") or result.artifact.summary
    ) or ""
    evidence = result.evidence
    return {
        "title": title,
        "summary": summary,
        "resource_type": resource_type,
        "resource_id": _optional_text(resource_id),
        "status": _optional_text(data.get("status")),
        "run_at": _optional_text(data.get("run_at") or data.get("publish_at")),
        "timezone": _optional_text(data.get("timezone")),
        "receipt_id": _optional_text(
            (evidence.receipt_id if evidence is not None else None)
            or result.tool_result.get("receipt_id")
        ),
        "external_operation_id": _optional_text(
            (evidence.external_operation_id if evidence is not None else None)
            or data.get("external_operation_id")
        ),
        "resource_refs": (
            list(evidence.resource_refs)
            if evidence is not None and evidence.resource_refs
            else list(result.artifact.resource_refs)
        ),
        "tool_call_id": _optional_text(
            evidence.tool_call_id if evidence is not None else None
        ),
    }


def _resource_key_for_type(artifact_type: str) -> str | None:
    if artifact_type in {"DRAFT", "POST_DRAFT", "CONTENT_DRAFT"}:
        return "draft_id"
    if artifact_type in {"SCHEDULE", "PUBLICATION_SCHEDULE"}:
        return "schedule_id"
    if artifact_type in {"POST", "PUBLISHED_POST", "PUBLICATION"}:
        return "post_id"
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered or None


__all__ = [
    "ArtifactStore",
    "ArtifactStorePort",
    "ArtifactStoreInterface",
    "MemoryArtifactStore",
    "MemoryStore",
    "PostgresArtifactStore",
]
