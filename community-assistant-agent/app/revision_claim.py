"""Durable revision claims for creator.revise_draft (no in-process locks)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import SideEffect

# Protocol debt (Creator still uses CREATE_CONTENT):
# TODO: REVISE_CONTENT + base_draft_id + expected_content_sha256 + revision_instruction

REVISION_CLAIM_STATUSES: frozenset[str] = frozenset(
    {
        "PREPARED",
        "IN_FLIGHT",
        "WAITING_DEPENDENCY",
        "UNKNOWN",
        "COMPLETED",
    }
)

REVISION_RELEASE_STATUSES: frozenset[str] = frozenset({"FAILED", "CANCELLED"})


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RevisionClaimConflict(LookupError):
    """Another revise already claimed this draft base version."""

    def __init__(
        self,
        message: str,
        *,
        existing_operation_key: str | None = None,
        existing_status: str | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_operation_key = existing_operation_key
        self.existing_status = existing_status


def revision_resource_id(
    *,
    user_id: str,
    draft_id: str,
    base_content_sha256: str,
) -> str:
    """Stable SideEffect.resource_id for a (user, draft, base sha) claim."""

    digest = _stable_hash(
        f"{user_id}|{draft_id}|{str(base_content_sha256).lower()}"
    )[:56]
    return f"rev:{digest}"


def revision_claim_lock_key(
    *,
    user_id: str,
    draft_id: str,
    base_content_sha256: str,
) -> str:
    return (
        f"assistant-revise-claim:{user_id}:"
        f"{draft_id}:{str(base_content_sha256).lower()}"
    )


async def acquire_revision_claim_lock(
    session: AsyncSession, *, lock_key: str
) -> None:
    """Transaction-scoped lock; PostgreSQL advisory, portable row mutex fallback."""

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock(hashtextextended(:claim_key, 0))"
            ),
            {"claim_key": lock_key},
        )
        return

    # SQLite / others: durable mutex row so empty claim tables still serialize.
    from sqlalchemy.exc import IntegrityError

    from app.database import IdempotencyRecord

    mutex_key = f"revise-claim:{_stable_hash(lock_key)[:80]}"
    existing = await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.user_id == "__revise_claim__",
            IdempotencyRecord.key == mutex_key,
        )
        .with_for_update()
    )
    if existing is None:
        async with session.begin_nested():
            session.add(
                IdempotencyRecord(
                    user_id="__revise_claim__",
                    key=mutex_key,
                    request_hash=_stable_hash(lock_key)[:64],
                    run_id="00000000-0000-0000-0000-000000000000",
                )
            )
            try:
                await session.flush()
            except IntegrityError:
                pass
        existing = await session.scalar(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.user_id == "__revise_claim__",
                IdempotencyRecord.key == mutex_key,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("failed to acquire revision claim mutex")
    # Holding FOR UPDATE on the mutex row until transaction end.


def claim_metadata_from_effect(effect: SideEffect) -> dict[str, Any]:
    result = dict(effect.result or {})
    claim = result.get("claim")
    if isinstance(claim, dict):
        return dict(claim)
    return {
        "source_draft_id": result.get("source_draft_id"),
        "base_content_sha256": result.get("base_content_sha256")
        or result.get("expected_content_sha256"),
        "user_id": result.get("user_id"),
    }


def effect_holds_revision_claim(
    effect: SideEffect,
    *,
    resource_id: str,
    base_content_sha256: str,
) -> bool:
    if effect.tool_name != "creator.revise_draft":
        return False
    if effect.resource_id != resource_id:
        return False
    if effect.status not in REVISION_CLAIM_STATUSES:
        return False
    meta = claim_metadata_from_effect(effect)
    base = str(
        meta.get("base_content_sha256")
        or meta.get("expected_content_sha256")
        or ""
    ).lower()
    if base and base != str(base_content_sha256).lower():
        return False
    return True


async def find_conflicting_revision_claim(
    session: AsyncSession,
    *,
    resource_id: str,
    base_content_sha256: str,
    exclude_operation_key: str,
) -> SideEffect | None:
    rows = list(
        (
            await session.scalars(
                select(SideEffect)
                .where(
                    SideEffect.tool_name == "creator.revise_draft",
                    SideEffect.resource_id == resource_id,
                    SideEffect.status.in_(sorted(REVISION_CLAIM_STATUSES)),
                    SideEffect.operation_key != exclude_operation_key,
                )
                .with_for_update()
            )
        ).all()
    )
    for row in rows:
        if effect_holds_revision_claim(
            row,
            resource_id=resource_id,
            base_content_sha256=base_content_sha256,
        ):
            return row
    return None


async def register_active_revision_claim(
    session: AsyncSession,
    *,
    resource_id: str,
    operation_key: str,
    run_id: str,
) -> None:
    """Insert durable unique claim row; IntegrityError means conflict."""

    from sqlalchemy.exc import IntegrityError

    from app.database import IdempotencyRecord

    claim_key = resource_id[:128]
    existing = await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.user_id == "__revise_active_claim__",
            IdempotencyRecord.key == claim_key,
        )
        .with_for_update()
    )
    if existing is not None:
        raise RevisionClaimConflict(
            "同一草稿版本已有进行中的修订，禁止并发提交",
            existing_operation_key=None,
            existing_status="CLAIMED",
        )
    try:
        async with session.begin_nested():
            session.add(
                IdempotencyRecord(
                    user_id="__revise_active_claim__",
                    key=claim_key,
                    request_hash=_stable_hash(operation_key)[:64],
                    run_id=run_id,
                )
            )
            await session.flush()
    except IntegrityError as exc:
        raise RevisionClaimConflict(
            "同一草稿版本已有进行中的修订，禁止并发提交",
            existing_operation_key=None,
            existing_status="CLAIMED",
        ) from exc


async def release_active_revision_claim(
    session: AsyncSession, *, resource_id: str | None
) -> None:
    if not resource_id:
        return
    from app.database import IdempotencyRecord

    row = await session.scalar(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.user_id == "__revise_active_claim__",
            IdempotencyRecord.key == resource_id[:128],
        )
        .with_for_update()
    )
    if row is not None:
        await session.delete(row)


def initial_revision_claim_payload(
    *,
    user_id: str,
    draft_id: str,
    base_content_sha256: str,
) -> dict[str, Any]:
    return {
        "claim": {
            "resource_type": "CONTENT_DRAFT",
            "source_draft_id": draft_id,
            "base_content_sha256": str(base_content_sha256).lower(),
            "user_id": user_id,
            # Protocol debt marker — not a native Creator REVISE_CONTENT call.
            "assistant_revision_semantics": True,
            "creator_kind": "CREATE_CONTENT",
        }
    }
