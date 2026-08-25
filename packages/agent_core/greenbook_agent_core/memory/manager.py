"""Canonical high-level Memory operations."""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from greenbook_agent_core.command.correction import CorrectionEvent

from .models import MemoryQuery, MemoryRecord, MemoryStatus, MemoryType
from .policy import MemoryWritePolicy
from .repository import InMemoryMemoryRepository

logger = logging.getLogger(__name__)


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

    @property
    def store(self) -> Any:
        return self._repository

    def remember(self, record: MemoryRecord) -> MemoryRecord:
        if record.source_type and record.source_id:
            existing = self._find_by_source(
                record.user_id,
                record.source_type,
                record.source_id,
                tenant_id=record.tenant_id,
            )
            if existing is not None:
                # The same logical fact (e.g. the same execution outcome
                # re-delivered by a retry) must not multiply into duplicate
                # rows: reuse the existing identity (memory_id / created_at)
                # and refresh the payload. The durable store upserts by
                # memory_id, so this is an in-place update, not an insert.
                updates = record.model_dump()
                updates["memory_id"] = existing.memory_id
                updates["created_at"] = existing.created_at
                updates["user_id"] = existing.user_id
                updates["access_count"] = existing.access_count
                updates["last_accessed_at"] = existing.last_accessed_at
                record = MemoryRecord.model_validate(updates)
        saved = self._repository.save(record)
        self._persist(saved)
        return saved

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

    def forget(self, memory_id: str) -> None:
        value = self._repository.delete(memory_id)
        if inspect.isawaitable(value):
            _run(value)
        if self._durable_repository is not None:
            value = self._durable_repository.delete(memory_id)
            if inspect.isawaitable(value):
                self._persist_awaitable(value, memory_id)

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
