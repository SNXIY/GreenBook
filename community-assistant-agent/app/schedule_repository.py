"""Shared schedule persistence for get/create/update/cancel reconcile."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.database import Database, ScheduledAction, utc_now

CANCELLABLE_STATUSES: frozenset[str] = frozenset({"SCHEDULED", "RETRYING"})


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ScheduleSnapshot:
    action_id: str
    draft_id: str
    expected_content_sha256: str
    run_at: datetime
    status: str
    capability_id: str | None = None
    idempotency_key: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    has_capability_token: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "draft_id": self.draft_id,
            "expected_content_sha256": self.expected_content_sha256,
            "run_at": as_utc(self.run_at).isoformat(),
            "status": self.status,
            "capability_id": self.capability_id,
            "idempotency_key": self.idempotency_key,
            "lease_owner": self.lease_owner,
            "lease_expires_at": (
                as_utc(self.lease_expires_at).isoformat()
                if self.lease_expires_at is not None
                else None
            ),
            "has_capability_token": self.has_capability_token,
        }

    def matches_fields(
        self,
        *,
        run_at: datetime,
        draft_id: str,
        expected_content_sha256: str,
    ) -> bool:
        return (
            as_utc(self.run_at) == as_utc(run_at)
            and self.draft_id == draft_id
            and self.expected_content_sha256.lower()
            == expected_content_sha256.lower()
        )


def snapshot_from_action(action: ScheduledAction) -> ScheduleSnapshot:
    return ScheduleSnapshot(
        action_id=action.id,
        draft_id=action.draft_id,
        expected_content_sha256=str(action.expected_content_sha256 or "").lower(),
        run_at=as_utc(action.run_at),
        status=action.status,
        capability_id=action.capability_id,
        idempotency_key=action.idempotency_key,
        lease_owner=action.lease_owner,
        lease_expires_at=(
            as_utc(action.lease_expires_at)
            if action.lease_expires_at is not None
            else None
        ),
        has_capability_token=bool(action.capability_token),
    )


class ScheduleRepository:
    """Local ScheduledAction is the authority for assistant schedules."""

    def __init__(
        self,
        database: Database,
        *,
        encrypt_token: Callable[[str], str] | None = None,
    ) -> None:
        self.database = database
        self._encrypt_token = encrypt_token

    async def get_own_schedule(
        self, *, action_id: str, user_id: str
    ) -> dict[str, Any]:
        async with self.database.sessions() as session:
            action = await session.scalar(
                select(ScheduledAction).where(
                    ScheduledAction.id == action_id,
                    ScheduledAction.user_id == user_id,
                )
            )
        if action is None:
            raise ValueError("定时发布任务不存在或不属于当前用户")
        return {
            "action_id": action.id,
            "draft_id": action.draft_id,
            "run_at": as_utc(action.run_at).isoformat(),
            "status": action.status,
        }

    async def read_snapshot(
        self, *, action_id: str, user_id: str
    ) -> ScheduleSnapshot | None:
        async with self.database.sessions() as session:
            action = await session.scalar(
                select(ScheduledAction).where(
                    ScheduledAction.id == action_id,
                    ScheduledAction.user_id == user_id,
                )
            )
        if action is None:
            return None
        return snapshot_from_action(action)

    async def cas_update(
        self,
        *,
        action_id: str,
        user_id: str,
        before: ScheduleSnapshot,
        target_run_at: datetime,
        target_draft_id: str,
        target_sha: str,
        capability_id: str,
        capability_token_plain: str,
    ) -> ScheduleSnapshot:
        """Atomically update schedule fields under row lock + before snapshot match."""

        encrypted = (
            self._encrypt_token(capability_token_plain)
            if self._encrypt_token is not None
            else capability_token_plain
        )
        async with self.database.sessions() as session, session.begin():
            action = await session.scalar(
                select(ScheduledAction)
                .where(
                    ScheduledAction.id == action_id,
                    ScheduledAction.user_id == user_id,
                )
                .with_for_update()
            )
            if action is None:
                raise ValueError("定时发布任务不存在或不属于当前用户")
            if action.status not in {"SCHEDULED", "RETRYING"}:
                raise ValueError(
                    f"定时发布任务当前状态为 {action.status}，不能修改"
                )
            current = snapshot_from_action(action)
            if (
                as_utc(current.run_at) != as_utc(before.run_at)
                or current.draft_id != before.draft_id
                or current.expected_content_sha256.lower()
                != before.expected_content_sha256.lower()
                or current.status != before.status
            ):
                raise LookupError(
                    "定时发布任务已被其他操作修改，请重新读取后再试"
                )
            action.run_at = as_utc(target_run_at)
            action.draft_id = target_draft_id
            action.expected_content_sha256 = target_sha.lower()
            action.status = "SCHEDULED"
            action.capability_id = capability_id
            action.capability_token = encrypted
            action.attempts = 0
            action.error = None
            action.lease_owner = None
            action.lease_expires_at = None
            action.updated_at = utc_now()
            await session.flush()
            return snapshot_from_action(action)

    async def get_by_idempotency_key(
        self, *, idempotency_key: str
    ) -> ScheduleSnapshot | None:
        async with self.database.sessions() as session:
            action = await session.scalar(
                select(ScheduledAction).where(
                    ScheduledAction.idempotency_key == idempotency_key
                )
            )
        if action is None:
            return None
        return snapshot_from_action(action)

    async def create_idempotent(
        self,
        *,
        run_id: str,
        user_id: str,
        draft_id: str,
        expected_content_sha256: str,
        run_at: datetime,
        idempotency_key: str,
        capability_id: str,
        capability_token_plain: str,
        instruction: str | None = None,
    ) -> tuple[ScheduleSnapshot, bool]:
        """Insert ScheduledAction; return (snapshot, created).

        If a row with the same idempotency_key already exists, return it and
        created=False without overwriting fields.
        """

        encrypted = (
            self._encrypt_token(capability_token_plain)
            if self._encrypt_token is not None
            else capability_token_plain
        )
        async with self.database.sessions() as session, session.begin():
            existing = await session.scalar(
                select(ScheduledAction)
                .where(ScheduledAction.idempotency_key == idempotency_key)
                .with_for_update()
            )
            if existing is not None:
                return snapshot_from_action(existing), False
            action = ScheduledAction(
                run_id=run_id,
                user_id=user_id,
                draft_id=draft_id,
                expected_content_sha256=expected_content_sha256.lower(),
                creator_task_id=None,
                instruction=instruction or "",
                run_at=as_utc(run_at),
                status="SCHEDULED",
                idempotency_key=idempotency_key,
                capability_id=capability_id,
                capability_token=encrypted,
            )
            session.add(action)
            await session.flush()
            return snapshot_from_action(action), True

    async def cancel_cas(
        self,
        *,
        action_id: str,
        user_id: str,
    ) -> tuple[str, ScheduleSnapshot, str | None]:
        """Cancel under row lock.

        Returns (outcome, snapshot, old_capability_id) where outcome is:
        cancelled | already_cancelled | already_executing | already_executed |
        terminal_failed | not_found
        """

        async with self.database.sessions() as session, session.begin():
            action = await session.scalar(
                select(ScheduledAction)
                .where(
                    ScheduledAction.id == action_id,
                    ScheduledAction.user_id == user_id,
                )
                .with_for_update()
            )
            if action is None:
                return "not_found", ScheduleSnapshot(
                    action_id=action_id,
                    draft_id="",
                    expected_content_sha256="",
                    run_at=utc_now(),
                    status="MISSING",
                ), None
            if action.status == "CANCELLED":
                return "already_cancelled", snapshot_from_action(action), None
            if action.status == "RUNNING":
                return "already_executing", snapshot_from_action(action), None
            if action.status == "COMPLETED":
                return "already_executed", snapshot_from_action(action), None
            if action.status == "FAILED":
                return "terminal_failed", snapshot_from_action(action), None
            if action.status not in CANCELLABLE_STATUSES:
                return "terminal_failed", snapshot_from_action(action), None
            old_capability_id = action.capability_id
            action.status = "CANCELLED"
            action.capability_token = None
            action.lease_owner = None
            action.lease_expires_at = None
            action.updated_at = utc_now()
            await session.flush()
            return "cancelled", snapshot_from_action(action), old_capability_id

    async def assert_publishable_for_worker(
        self,
        *,
        action_id: str,
        worker_id: str,
        now: datetime | None = None,
    ) -> ScheduleSnapshot | None:
        """Pre-publish gate: RUNNING + matching unexpired lease + token present."""

        current = now or utc_now()
        async with self.database.sessions() as session, session.begin():
            action = await session.scalar(
                select(ScheduledAction)
                .where(ScheduledAction.id == action_id)
                .with_for_update()
            )
            if action is None:
                return None
            if action.status != "RUNNING":
                return None
            if action.lease_owner != worker_id:
                return None
            if action.lease_expires_at is None or as_utc(action.lease_expires_at) <= as_utc(
                current
            ):
                return None
            if not action.capability_token:
                return None
            return snapshot_from_action(action)
