"""ToolExecutionLedger — idempotency guard and invocation audit trail.

Phase 4.3: in-memory.  Each unique idempotency_key produces exactly
one entry; subsequent calls with the same key return the cached result.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from ..evidence import ExecutionEvidence
from .invocation_context import ToolInvocationContext


class InvocationStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class LedgerEntry(BaseModel):
    """One recorded tool invocation."""

    invocation_id: str = ""
    idempotency_key: str = ""
    tool_name: str = ""
    capability: str = ""
    task_id: str = ""
    execution_id: str = ""
    step_id: str = ""

    status: InvocationStatus = InvocationStatus.PENDING

    result: dict[str, Any] = {}       # cached tool result
    error_code: str = ""
    error_message: str = ""

    started_at: str = ""
    finished_at: str = ""
    duration_ms: float = 0.0
    evidence: ExecutionEvidence | None = None


class ToolExecutionLedger:
    """Idempotency guard — one invocation per key."""

    def __init__(self) -> None:
        self._entries: dict[str, LedgerEntry] = {}
        self._by_key: dict[str, str] = {}  # idempotency_key → invocation_id

    # ── lookup ──

    def find_by_key(self, idempotency_key: str) -> LedgerEntry | None:
        inv_id = self._by_key.get(idempotency_key)
        if inv_id is None:
            return None
        return self._entries.get(inv_id)

    def find_by_id(self, invocation_id: str) -> LedgerEntry | None:
        return self._entries.get(invocation_id)

    # ── lifecycle ──

    def record_start(
        self,
        ctx: ToolInvocationContext,
        evidence: ExecutionEvidence | None = None,
    ) -> LedgerEntry:
        """Create a PENDING→RUNNING entry.  Raises if key already used."""
        existing = self.find_by_key(ctx.idempotency_key)
        if existing is not None:
            raise _duplicate_key(ctx.idempotency_key, existing)

        entry = LedgerEntry(
            invocation_id=ctx.invocation_id,
            idempotency_key=ctx.idempotency_key,
            tool_name=ctx.tool_name,
            capability=ctx.capability,
            task_id=ctx.task_id,
            execution_id=ctx.execution_id,
            step_id=ctx.step_id,
            status=InvocationStatus.RUNNING,
            started_at=datetime.now(UTC).isoformat(),
            evidence=evidence or ExecutionEvidence.from_context(ctx),
        )
        self._entries[entry.invocation_id] = entry
        self._by_key[entry.idempotency_key] = entry.invocation_id
        return entry

    def record_complete(
        self,
        invocation_id: str,
        result: dict[str, Any],
        duration_ms: float,
        *,
        evidence: ExecutionEvidence | None = None,
    ) -> LedgerEntry:
        entry = self._require(invocation_id)
        entry.status = InvocationStatus.COMPLETED
        entry.result = result
        entry.finished_at = datetime.now(UTC).isoformat()
        entry.duration_ms = duration_ms
        if evidence is not None:
            entry.evidence = evidence
        return entry

    def record_failure(
        self,
        invocation_id: str,
        error_code: str,
        error_message: str,
        duration_ms: float,
        result: dict[str, Any] | None = None,
        *,
        evidence: ExecutionEvidence | None = None,
    ) -> LedgerEntry:
        entry = self._require(invocation_id)
        entry.status = InvocationStatus.FAILED
        entry.error_code = error_code
        entry.error_message = error_message
        if result is not None:
            entry.result = dict(result)
        entry.finished_at = datetime.now(UTC).isoformat()
        entry.duration_ms = duration_ms
        if evidence is not None:
            entry.evidence = evidence
        return entry

    def record_timeout(
        self,
        invocation_id: str,
        duration_ms: float,
        result: dict[str, Any] | None = None,
        *,
        evidence: ExecutionEvidence | None = None,
    ) -> LedgerEntry:
        entry = self._require(invocation_id)
        entry.status = InvocationStatus.TIMEOUT
        entry.error_code = "TIMEOUT"
        entry.error_message = "Tool invocation timed out"
        if result is not None:
            entry.result = dict(result)
        entry.finished_at = datetime.now(UTC).isoformat()
        entry.duration_ms = duration_ms
        if evidence is not None:
            entry.evidence = evidence
        return entry

    def record_evidence(
        self,
        invocation_id: str,
        evidence: ExecutionEvidence,
    ) -> LedgerEntry:
        """Update the observed evidence without changing lifecycle status."""

        entry = self._require(invocation_id)
        entry.evidence = evidence
        return entry

    # ── replay ──

    def try_replay(
        self, idempotency_key: str,
    ) -> LedgerEntry | None:
        """If *idempotency_key* was already executed, return the cached
        entry.  Returns None when this is a new key."""
        entry = self.find_by_key(idempotency_key)
        if entry is None:
            return None
        if entry.status == InvocationStatus.COMPLETED:
            return entry
        # FAILED / TIMEOUT entries are NOT replayed — the caller must
        # decide whether to retry with a new invocation_id.
        return None

    # ── queries ──

    def list_by_execution(self, execution_id: str) -> list[LedgerEntry]:
        return [e for e in self._entries.values()
                if e.execution_id == execution_id]

    def count(self) -> int:
        return len(self._entries)

    def _require(self, invocation_id: str) -> LedgerEntry:
        entry = self._entries.get(invocation_id)
        if entry is None:
            raise ValueError(f"Ledger entry not found: {invocation_id}")
        return entry


def _duplicate_key(key: str, existing: LedgerEntry) -> ValueError:
    return ValueError(
        f"Idempotency key '{key}' already used by invocation "
        f"'{existing.invocation_id}' (status={existing.status})"
    )
