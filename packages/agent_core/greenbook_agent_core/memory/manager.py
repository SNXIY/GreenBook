"""Canonical high-level Memory operations."""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from datetime import UTC, datetime
from typing import Any

from greenbook_agent_core.command.correction import CorrectionEvent

from .models import MemoryQuery, MemoryRecord, MemoryStatus, MemoryType
from .policy import MemoryWritePolicy
from .repository import InMemoryMemoryRepository

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _merge_semantic_metadata(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    source_id: str | None,
) -> dict[str, Any]:
    metadata = dict(existing)
    metadata.update({
        key: incoming[key]
        for key in (
            "memory_contract",
            "memory_version",
            "memory_role",
            "subject",
            "predicate",
            "object",
            "normalized_fact",
            "observed_at",
        )
        if key in incoming
    })
    metadata["evidence_count"] = int(metadata.get("evidence_count", 1) or 1) + 1
    source_ids = list(metadata.get("source_ids") or [])
    for value in (
        existing.get("provenance", {}).get("source_id")
        if isinstance(existing.get("provenance"), dict)
        else None,
        source_id,
    ):
        if value and value not in source_ids:
            source_ids.append(value)
    if source_ids:
        metadata["source_ids"] = source_ids[-20:]
    provenance = dict(existing.get("provenance") or {})
    provenance.update(dict(incoming.get("provenance") or {}))
    metadata["provenance"] = provenance
    return metadata


class MemoryManager:
    """Write-policy-aware facade over the canonical MemoryRepository."""

    def __init__(
        self,
        repository: Any | None = None,
        *,
        write_policy: MemoryWritePolicy | None = None,
        durable_repository: Any | None = None,
    ) -> None:
        self._repository = repository or InMemoryMemoryRepository()
        self._write_policy = write_policy or MemoryWritePolicy()
        self._durable_repository = durable_repository
        self._semantic_lock = threading.RLock()

    @property
    def store(self) -> Any:
        return self._repository

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        source_existing: MemoryRecord | None = None
        if record.source_type and record.source_id:
            source_existing = self._find_by_source(
                record.user_id,
                record.source_type,
                record.source_id,
                tenant_id=record.tenant_id,
            )
            if source_existing is not None and record.memory_type == MemoryType.PREFERENCE:
                # A superseded/inactive preference is historical evidence.  A
                # retried source event must never resurrect it as active.
                if source_existing.status != MemoryStatus.ACTIVE:
                    return source_existing
                if self._preference_identity(source_existing) == self._preference_identity(record):
                    record = record.model_copy(update={
                        "memory_id": source_existing.memory_id,
                        "created_at": source_existing.created_at,
                        "access_count": source_existing.access_count,
                        "last_accessed_at": source_existing.last_accessed_at,
                    })
            elif source_existing is not None:
                # The same logical fact (e.g. the same execution outcome
                # re-delivered by a retry) must not multiply into duplicate
                # rows: reuse the existing identity (memory_id / created_at)
                # and refresh the payload. The durable store upserts by
                # memory_id, so this is an in-place update, not an insert.
                updates = record.model_dump()
                updates["memory_id"] = source_existing.memory_id
                updates["created_at"] = source_existing.created_at
                updates["user_id"] = source_existing.user_id
                updates["tenant_id"] = source_existing.tenant_id
                updates["access_count"] = source_existing.access_count
                updates["last_accessed_at"] = source_existing.last_accessed_at
                record = MemoryRecord.model_validate(updates)
        if (
            record.memory_type == MemoryType.PREFERENCE
            and record.status == MemoryStatus.ACTIVE
            and self._preference_identity(record) is not None
        ):
            return self._remember_preference(record)
        saved = self._repository.save(record)
        self._persist(saved)
        return saved

    def remember_semantic(
        self,
        record: MemoryRecord,
        *,
        subject: str,
        predicate: str,
        object_value: str,
    ) -> MemoryRecord:
        """Atomically project one Semantic fact through the canonical store.

        Semantic and Preference share the ``SEMANTIC`` storage enum value, so
        the explicit contract/role metadata is part of this boundary.  A
        repository that implements ``replace_semantic`` can perform the
        supersede and upsert in one transaction; the lock plus conservative
        fallback keeps compatible in-process repositories safe as well.
        """

        metadata = record.metadata
        if (
            record.memory_type != MemoryType.SEMANTIC
            or metadata.get("memory_contract") != "SEMANTIC_V1"
            or metadata.get("memory_role") != "stable_fact"
            or str(metadata.get("subject") or "") != str(subject)
            or str(metadata.get("predicate") or "") != str(predicate)
            or str(metadata.get("object") or "") != str(object_value)
            or record.status != MemoryStatus.ACTIVE
        ):
            raise ValueError("SEMANTIC_MEMORY_CONTRACT_REQUIRED")
        scope_user = str(record.user_id or "").strip()
        scope_tenant = str(record.tenant_id or "").strip()
        if not scope_user or not scope_tenant:
            raise ValueError("SEMANTIC_MEMORY_SCOPE_REQUIRED")

        with self._semantic_lock:
            replacer = getattr(self._repository, "replace_semantic", None)
            if callable(replacer):
                saved = replacer(
                    record,
                    subject=subject,
                    predicate=predicate,
                    object_value=object_value,
                )
                if inspect.isawaitable(saved):
                    saved = _run(saved)
            else:
                saved = self._remember_semantic_fallback(
                    record,
                    subject=subject,
                    predicate=predicate,
                    object_value=object_value,
                )
            self._persist_semantic(
                saved if isinstance(saved, MemoryRecord) else record,
                subject=subject,
                predicate=predicate,
                object_value=object_value,
            )
            return saved

    def _remember_semantic_fallback(
        self,
        record: MemoryRecord,
        *,
        subject: str,
        predicate: str,
        object_value: str,
    ) -> MemoryRecord:
        values = self._repository.search(MemoryQuery(
            user_id=record.user_id,
            tenant_id=record.tenant_id,
            type=MemoryType.SEMANTIC,
            status=MemoryStatus.ACTIVE,
            metadata_filters={
                "memory_contract": "SEMANTIC_V1",
                "memory_role": "stable_fact",
                "subject": subject,
                "predicate": predicate,
            },
            limit=100,
            sort_by="created_at",
        ))
        if inspect.isawaitable(values):
            values = _run(values)
        active = [
            item if isinstance(item, MemoryRecord) else MemoryRecord.model_validate(item)
            for item in (values or ())
        ]
        same = next(
            (item for item in active if str(item.metadata.get("object") or "") == object_value),
            None,
        )
        if same is not None:
            metadata = _merge_semantic_metadata(same.metadata, record.metadata, record.source_id)
            merged = same.model_copy(update={
                "content": record.content or same.content,
                "structured_metadata": metadata,
                "confidence": max(same.confidence, record.confidence),
                "importance": max(same.importance, record.importance),
                "updated_at": _now_iso(),
            })
            for item in active:
                if item.memory_id == same.memory_id:
                    continue
                self._repository.save(item.model_copy(update={
                    "status": MemoryStatus.SUPERSEDED,
                    "structured_metadata": {
                        **item.metadata,
                        "replacement_memory_id": same.memory_id,
                    },
                    "updated_at": _now_iso(),
                }))
            return self._repository.save(merged)

        for item in active:
            self._repository.save(item.model_copy(update={
                "status": MemoryStatus.SUPERSEDED,
                "structured_metadata": {
                    **item.metadata,
                    "replacement_memory_id": record.memory_id,
                },
                "updated_at": _now_iso(),
            }))
        return self._repository.save(record)

    def _persist_semantic(
        self,
        record: MemoryRecord,
        *,
        subject: str,
        predicate: str,
        object_value: str,
    ) -> None:
        if self._durable_repository is None:
            return
        replacer = getattr(self._durable_repository, "replace_semantic", None)
        if callable(replacer):
            try:
                value = replacer(
                    record,
                    subject=subject,
                    predicate=predicate,
                    object_value=object_value,
                )
            except Exception:  # noqa: BLE001 - durable shadow must not break the turn
                logger.warning(
                    "Durable semantic memory persistence failed memory_id=%s",
                    record.memory_id,
                    exc_info=True,
                )
                return
            if inspect.isawaitable(value):
                self._persist_awaitable(value, record.memory_id)
            return
        # Compatibility repositories that predate semantic replacement still
        # receive the active projection.  Production Postgres implements the
        # atomic replace operation above.
        self._persist(record)

    def recall(self, query: MemoryQuery) -> list[MemoryRecord]:
        results = self._repository.search(query)
        if inspect.isawaitable(results):
            results = _run(results)
        touched: list[MemoryRecord] = []
        for record in results:
            value = self._repository.touch(record.memory_id)
            if inspect.isawaitable(value):
                value = _run(value)
            touched.append(value or record)
        return touched

    def forget(
        self,
        memory_id: str,
        *,
        user_id: str | None = None,
        tenant_id: str | None = None,
    ) -> None:
        value = self._delete(
            memory_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        if inspect.isawaitable(value):
            _run(value)
        if self._durable_repository is not None:
            try:
                value = self._durable_repository.delete(
                    memory_id,
                    user_id=user_id,
                    tenant_id=tenant_id,
                )
            except TypeError:
                value = self._durable_repository.delete(memory_id)
            if inspect.isawaitable(value):
                self._persist_awaitable(value, memory_id)

    def deactivate(
        self,
        memory_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> MemoryRecord | None:
        """Make one memory unavailable to recall while retaining its audit row."""

        return self._set_status(
            memory_id,
            MemoryStatus.INACTIVE,
            user_id=user_id,
            tenant_id=tenant_id,
        )

    def supersede(
        self,
        memory_id: str,
        *,
        user_id: str,
        tenant_id: str,
        replacement_memory_id: str | None = None,
    ) -> MemoryRecord | None:
        """Retain a historical preference after a newer value wins."""

        return self._set_status(
            memory_id,
            MemoryStatus.SUPERSEDED,
            user_id=user_id,
            tenant_id=tenant_id,
            replacement_memory_id=replacement_memory_id,
        )

    def remember_execution(
        self,
        user_id: str,
        goal: str = "",
        category: str = "",
        status: str = "",
        draft_id: str | None = None,
        schedule_id: str | None = None,
        **extra: Any,
    ) -> MemoryRecord:
        event = {
            "event_type": "TASK_COMPLETED" if status == "COMPLETED" else "TASK_FAILED_MAJOR",
            "artifact_id": draft_id or schedule_id,
        }
        if not self._write_policy.should_write(event):
            raise ValueError("MEMORY_WRITE_POLICY_REJECTED")
        return self.remember(MemoryRecord(
            user_id=user_id,
            task_id=extra.get("task_id"),
            source_type="EXECUTION_OUTCOME",
            source_id=extra.get("execution_id"),
            memory_type=MemoryType.EPISODIC,
            content=f"[{status}] {goal}",
            structured_metadata={
                "goal": goal,
                "goal_category": category,
                "status": status,
                "draft_id": draft_id,
                "schedule_id": schedule_id,
                **extra,
            },
            importance=0.7 if status == "COMPLETED" else 0.5,
        ))

    def remember_preference(
        self,
        user_id: str,
        preference_type: str,
        value: str,
        confidence: float = 0.5,
        *,
        tenant_id: str = "",
        source_conversation_id: str | None = None,
        source_type: str = "USER_EXPLICIT_PREFERENCE",
    ) -> MemoryRecord:
        return self.remember(MemoryRecord(
            user_id=user_id,
            tenant_id=tenant_id,
            source_conversation_id=source_conversation_id,
            memory_type=MemoryType.PREFERENCE,
            status=MemoryStatus.ACTIVE,
            content=f"Prefers {preference_type}: {value}",
            structured_metadata={
                "preference_type": preference_type,
                "value": value,
                "confidence": confidence,
            },
            importance=min(confidence * 0.8, 0.9),
            confidence=confidence,
            source_type=source_type,
        ))

    def remember_pattern(
        self,
        user_id: str,
        pattern: str,
        success: bool = True,
        context: dict[str, Any] | None = None,
    ) -> MemoryRecord:
        if not self._write_policy.should_write({"event_type": "REUSABLE_STRATEGY"}):
            raise ValueError("MEMORY_WRITE_POLICY_REJECTED")
        return self.remember(MemoryRecord(
            user_id=user_id,
            memory_type=MemoryType.PROCEDURAL,
            source_type="REUSABLE_STRATEGY",
            content=pattern,
            structured_metadata={"success": success, "context": context or {}},
            importance=0.3,
        ))

    def remember_correction(self, event: CorrectionEvent) -> MemoryRecord | None:
        if not self._write_policy.should_write({"event_type": "USER_CORRECTION"}):
            return None
        return self.remember(MemoryRecord(
            user_id=event.user_id,
            conversation_id=event.conversation_id or None,
            task_id=event.task_id,
            memory_type=(MemoryType.PREFERENCE if event.preference_candidate else MemoryType.EPISODIC),
            content=event.correction_summary,
            structured_metadata={
                "event_type": "USER_CORRECTION",
                "original_target": event.original_target,
                "corrected_target": event.corrected_target,
                "preference_candidate": event.preference_candidate,
            },
            source_type="USER_CORRECTION",
            source_id=event.event_id,
            importance=0.55 if event.preference_candidate else 0.4,
            confidence=0.8,
        ))

    def _find_by_source(
        self,
        user_id: str,
        source_type: str,
        source_id: str,
        *,
        tenant_id: str = "",
    ) -> MemoryRecord | None:
        finder = getattr(self._repository, "find_by_source", None)
        if finder is None:
            return None
        try:
            value = finder(
                user_id,
                source_type,
                source_id,
                tenant_id=tenant_id,
            )
        except TypeError:
            # Compatibility with injected repositories that predate tenant
            # scope; production repositories implement the scoped signature.
            value = finder(user_id, source_type, source_id)
        if inspect.isawaitable(value):
            value = _run(value)
        return value

    def _remember_preference(self, record: MemoryRecord) -> MemoryRecord:
        """Merge one preference identity without creating active duplicates."""

        active = self._active_preferences(record)
        identity = self._preference_identity(record)
        same_value = [
            item for item in active
            if self._preference_identity(item) == identity
        ]
        if same_value:
            existing = same_value[0]
            metadata = dict(existing.metadata)
            metadata["evidence_count"] = int(metadata.get("evidence_count", 1) or 1) + 1
            self._append_provenance(
                metadata,
                existing=existing,
                incoming=record,
            )
            merged = existing.model_copy(update={
                "content": record.content or existing.content,
                "structured_metadata": metadata,
                "confidence": max(existing.confidence, record.confidence),
                "importance": max(existing.importance, record.importance),
                "updated_at": _now_iso(),
            })
            return self._save(merged)

        preference_key = self._preference_key(record)
        if preference_key:
            # A new value for one preference key wins the active projection,
            # but old values remain queryable as superseded history.
            for item in active:
                if self._preference_key(item) != preference_key:
                    continue
                self._save(item.model_copy(update={
                    "status": MemoryStatus.SUPERSEDED,
                    "updated_at": _now_iso(),
                }))
        return self._save(record)

    def _active_preferences(self, record: MemoryRecord) -> list[MemoryRecord]:
        search = getattr(self._repository, "search", None)
        if not callable(search):
            return []
        try:
            values = search(MemoryQuery(
                user_id=record.user_id,
                tenant_id=record.tenant_id,
                type=MemoryType.PREFERENCE,
                status=MemoryStatus.ACTIVE,
                limit=100,
                sort_by="created_at",
            ))
        except Exception:  # noqa: BLE001 - lifecycle remains best effort for injected stores
            logger.warning("preference_lifecycle_search_failed", exc_info=True)
            return []
        if inspect.isawaitable(values):
            try:
                values = _run(values)
            except RuntimeError:
                # MemoryManager is synchronous by contract.  Production uses
                # its synchronous in-process primary and persists a durable
                # shadow; an async-only injected repository cannot be merged
                # from this call path without changing that contract.
                return []
        return [
            item if isinstance(item, MemoryRecord) else MemoryRecord.model_validate(item)
            for item in (values or ())
        ]

    @staticmethod
    def _preference_key(record: MemoryRecord) -> str:
        return str(record.metadata.get("preference_type") or "").strip().casefold()

    @classmethod
    def _preference_identity(cls, record: MemoryRecord) -> tuple[str, str] | None:
        key = cls._preference_key(record)
        value = str(record.metadata.get("value") or "").strip().casefold()
        if not key or not value:
            return None
        return key, value

    @staticmethod
    def _append_provenance(
        metadata: dict[str, Any],
        *,
        existing: MemoryRecord,
        incoming: MemoryRecord,
    ) -> None:
        conversations = list(metadata.get("source_conversation_ids") or [])
        for value in (existing.source_conversation_id, incoming.source_conversation_id):
            if value and value not in conversations:
                conversations.append(value)
        if conversations:
            metadata["source_conversation_ids"] = conversations[-20:]
        source_ids = list(metadata.get("source_ids") or [])
        for value in (existing.source_id, incoming.source_id):
            if value and value not in source_ids:
                source_ids.append(value)
        if source_ids:
            metadata["source_ids"] = source_ids[-20:]

    def _save(self, record: MemoryRecord) -> MemoryRecord:
        saved = self._repository.save(record)
        if inspect.isawaitable(saved):
            try:
                saved = _run(saved)
            except RuntimeError:
                # Keep the contract synchronous for the existing in-process
                # primary.  Async-only repositories are handled by their own
                # caller; do not return a coroutine as a MemoryRecord.
                return record
        self._persist(saved)
        return saved

    def _get_scoped(
        self,
        memory_id: str,
        *,
        user_id: str,
        tenant_id: str,
    ) -> MemoryRecord | None:
        getter = getattr(self._repository, "get", None)
        if not callable(getter):
            getter = getattr(self._repository, "find_by_id", None)
        if not callable(getter):
            return None
        try:
            value = getter(memory_id, user_id=user_id, tenant_id=tenant_id)
        except TypeError:
            value = getter(memory_id)
        if inspect.isawaitable(value):
            try:
                value = _run(value)
            except RuntimeError:
                return None
        return value

    def _set_status(
        self,
        memory_id: str,
        status: MemoryStatus,
        *,
        user_id: str,
        tenant_id: str,
        replacement_memory_id: str | None = None,
    ) -> MemoryRecord | None:
        record = self._get_scoped(
            memory_id,
            user_id=user_id,
            tenant_id=tenant_id,
        )
        if record is None:
            return None
        metadata = dict(record.metadata)
        if replacement_memory_id:
            metadata["replacement_memory_id"] = replacement_memory_id
        return self._save(record.model_copy(update={
            "status": status,
            "structured_metadata": metadata,
            "updated_at": _now_iso(),
        }))

    def _delete(
        self,
        memory_id: str,
        *,
        user_id: str | None,
        tenant_id: str | None,
    ) -> Any:
        delete = getattr(self._repository, "delete", None)
        if not callable(delete):
            return None
        try:
            return delete(
                memory_id,
                user_id=user_id,
                tenant_id=tenant_id,
            )
        except TypeError:
            return delete(memory_id)

    def _persist(self, record: MemoryRecord) -> None:
        if self._durable_repository is None:
            return
        try:
            value = self._durable_repository.save(record)
        except Exception:
            # A synchronous durable failure must be visible, not silent: the
            # memory stays in the in-process store for this session, but the
            # operator sees exactly what failed (design goal 0813 — no
            # silently dropped persistence).
            logger.warning(
                "Durable memory persistence failed synchronously "
                "memory_id=%s source_type=%s source_id=%s",
                record.memory_id,
                record.source_type,
                record.source_id,
                exc_info=True,
            )
            return
        if inspect.isawaitable(value):
            self._persist_awaitable(value, record.memory_id)

    @staticmethod
    def _persist_awaitable(value: Any, memory_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(value)
            except Exception:
                logger.warning(
                    "Durable memory persistence failed (sync path) memory_id=%s",
                    memory_id,
                    exc_info=True,
                )
            return
        loop.create_task(_guard_persist(value, memory_id))


async def _guard_persist(value: Any, memory_id: str) -> None:
    """Run a durable save without letting a failure vanish into a
    fire-and-forget task: the error is logged and the memory simply remains
    in-process for the current session."""
    try:
        await value
    except Exception:
        logger.warning(
            "Durable memory persistence failed; memory kept in-process "
            "memory_id=%s",
            memory_id,
            exc_info=True,
        )


def _run(value: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(value)
    raise RuntimeError("Synchronous MemoryManager cannot await inside an active loop")


__all__ = ["MemoryManager"]
